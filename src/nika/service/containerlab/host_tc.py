"""Containerlab controller-side traffic-control helpers for host veth endpoints.

The lab nodes never execute these commands.  This keeps fault-injection state
outside the namespace exposed to troubleshooting agents.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import subprocess

from nika.runtime.base import RuntimeCapabilityError
from nika.runtime.shared.link_faults import corrupt_commands
from nika.utils.network_change_log import log_network_change

# Every host command here is a short iproute2/tc/nsenter call; a stuck sudo
# prompt or wedged netlink call must not hang fault injection forever.
_HOST_CMD_TIMEOUT_SEC = 60.0


class HostTcController:
    """Apply a qdisc to the host peer of a lab-node interface."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime

    @staticmethod
    def _run(*args: str) -> str:
        try:
            result = subprocess.run(
                ("sudo", "-n", *args),
                check=False,
                capture_output=True,
                text=True,
                timeout=_HOST_CMD_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeCapabilityError(
                f"host command timed out after {_HOST_CMD_TIMEOUT_SEC:.0f}s "
                f"({' '.join(args)})"
            ) from exc
        except FileNotFoundError as exc:
            raise RuntimeCapabilityError(
                f"host command is unavailable: {args[0]!r}. "
                "controller-host iproute2 (tc) is required."
            ) from exc
        if result.returncode:
            raise RuntimeCapabilityError(
                f"host command failed ({' '.join(args)}): {result.stderr.strip()}"
            )
        return result.stdout

    def peer_name(self, node: str, intf: str) -> str:
        """Resolve ``intf``'s host-side veth by its peer ifindex.

        Interface indexes are per network namespace, so a bare ifindex match
        can hit an unrelated host interface. The host row must also be a veth
        whose own peer (``@if<N>``) is ``intf``'s ifindex and whose peer lives
        in another namespace (``link-netnsid``).
        """
        pid = self._container_pid(node)
        indexes = self._run(
            "nsenter",
            "-t",
            str(pid),
            "-m",
            "-n",
            "cat",
            f"/sys/class/net/{intf}/iflink",
            f"/sys/class/net/{intf}/ifindex",
        ).split()
        if len(indexes) != 2 or not all(value.isdecimal() for value in indexes):
            raise RuntimeCapabilityError(
                f"could not resolve host peer for {node}:{intf}"
            )
        iflink, ifindex = indexes
        peer = host_veth_for(self._run("ip", "-o", "link", "show"), iflink, ifindex)
        if peer is not None:
            return peer
        raise RuntimeCapabilityError(
            f"host veth ifindex {iflink} for {node}:{intf} is absent"
        )

    def wire_partner(self, peer: str) -> str | None:
        """Other host end of the wire ``peer`` belongs to (``wires.py``), if any."""
        filters = self._run("tc", "filter", "show", "dev", peer, "ingress")
        match = re.search(r"redirect to device (\S+)\)", filters)
        return match.group(1) if match else None

    def wire_ends(self, peer: str) -> list[str]:
        """``peer`` and, on a wired link, the far end: a cable has two ends."""
        partner = self.wire_partner(peer)
        return [peer] if partner is None else [peer, partner]

    def egress_dev(self, node: str, intf: str) -> str:
        """Host device that carries what ``node`` sends out of ``intf``."""
        peer = self.peer_name(node, intf)
        return self.wire_partner(peer) or peer

    def set_link_up(self, peer: str) -> None:
        for end in self.wire_ends(peer):
            self._run("ip", "link", "set", "dev", end, "up")

    def set_corrupt(self, node: str, intf: str, percentage: int) -> str:
        """Corrupt what ``node`` sends out of ``intf`` on its host-side wire end.

        Returns the host device. See ``corrupt_commands`` for how frames fail.
        """
        dev = self.egress_dev(node, intf)
        self._run("sh", "-c", " && ".join(corrupt_commands(dev, percentage)))
        log_network_change(
            f"host_tc corrupt on {node}:{intf} dev={dev} corrupt={percentage}%",
            mechanism="host_tc",
            host=node,
            intf=intf,
            peer=dev,
            params={"corrupt": percentage},
            action="netem_corrupt",
        )
        return dev

    def corrupt_configured(self, dev: str) -> bool:
        qdisc = self.qdisc(dev).lower()
        return "netem" in qdisc and "corrupt" in qdisc

    def set_link_down(self, node: str, intf: str) -> tuple[str, str]:
        """Bring down a link so endpoints report operstate down.

        Prefer the host-side veth peer (carrier loss). Fall back to setting the
        interface down inside the node namespace when no host peer exists.
        """
        try:
            peer = self.peer_name(node, intf)
        except RuntimeCapabilityError:
            self.set_node_link_down(node, intf)
            log_network_change(
                f"host_tc link down via node intf {node}:{intf}",
                mechanism="host_tc",
                host=node,
                intf=intf,
                action="link_down",
                mode="node_intf",
            )
            return "node_intf", f"{node}:{intf}"
        for end in self.wire_ends(peer):
            self._run("ip", "link", "set", "dev", end, "down")
        log_network_change(
            f"host_tc link down via host peer {peer} for {node}:{intf}",
            mechanism="host_tc",
            host=node,
            intf=intf,
            peer=peer,
            action="link_down",
            mode="host_peer",
        )
        return "host_peer", peer

    def set_node_link_down(self, node: str, intf: str) -> None:
        pid = self._container_pid(node)
        self._run(
            "nsenter",
            "-t",
            str(pid),
            "-n",
            "ip",
            "link",
            "set",
            "dev",
            intf,
            "down",
        )

    def set_node_link_up(self, node: str, intf: str) -> None:
        pid = self._container_pid(node)
        self._run(
            "nsenter",
            "-t",
            str(pid),
            "-n",
            "ip",
            "link",
            "set",
            "dev",
            intf,
            "up",
        )

    def link_peer_down(self, peer: str) -> bool:
        output = self._run("ip", "link", "show", "dev", peer).lower()
        return "state down" in output

    def set_netem_loss(self, node: str, intf: str, percentage: int) -> str:
        peer = self.peer_name(node, intf)
        self._run(
            "tc",
            "qdisc",
            "replace",
            "dev",
            peer,
            "root",
            "netem",
            "loss",
            f"{percentage}%",
        )
        log_network_change(
            f"host_tc netem loss on {node}:{intf} peer={peer} loss={percentage}%",
            mechanism="host_tc",
            host=node,
            intf=intf,
            peer=peer,
            params={"loss": percentage},
            action="netem_loss",
        )
        return peer

    def set_tbf(
        self,
        node: str,
        intf: str,
        rate: str,
        burst: str,
        limit: str,
    ) -> str:
        """Apply TBF to what ``node`` sends out of ``intf``, outside the node.

        On a wired link the far host end carries that traffic; without a host
        peer, the qdisc goes inside the node.
        """
        try:
            peer = self.egress_dev(node, intf)
        except RuntimeCapabilityError:
            pid = self._container_pid(node)
            self._run(
                "nsenter",
                "-t",
                str(pid),
                "-n",
                "tc",
                "qdisc",
                "replace",
                "dev",
                intf,
                "root",
                "tbf",
                "rate",
                rate,
                "burst",
                burst,
                "limit",
                limit,
            )
            target = f"node:{node}:{intf}"
            log_network_change(
                f"host_tc tbf on {node}:{intf} (node ns) "
                f"rate={rate} burst={burst} limit={limit}",
                mechanism="host_tc",
                host=node,
                intf=intf,
                peer=target,
                params={"rate": rate, "burst": burst, "limit": limit},
                action="tbf",
                mode="node_intf",
            )
            return target
        self._run(
            "tc",
            "qdisc",
            "replace",
            "dev",
            peer,
            "root",
            "tbf",
            "rate",
            rate,
            "burst",
            burst,
            "limit",
            limit,
        )
        log_network_change(
            f"host_tc tbf on {node}:{intf} peer={peer} "
            f"rate={rate} burst={burst} limit={limit}",
            mechanism="host_tc",
            host=node,
            intf=intf,
            peer=peer,
            params={"rate": rate, "burst": burst, "limit": limit},
            action="tbf",
            mode="host_peer",
        )
        return peer

    def qdisc(self, peer: str) -> str:
        return self._run("tc", "qdisc", "show", "dev", peer)

    def clear(self, peer: str) -> None:
        # A missing qdisc is the normal result after a lab has been destroyed.
        try:
            subprocess.run(
                ["sudo", "-n", "tc", "qdisc", "del", "dev", peer, "root"],
                check=False,
                capture_output=True,
                text=True,
                timeout=_HOST_CMD_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            print(f"WARNING: clearing host qdisc on {peer} timed out")

    def start_link_flap(self, peer: str, down_time: int, up_time: int) -> None:
        """Flap a host-side veth without putting fault state in the lab node."""
        pid_path = self._flap_pid_path(peer)
        self.stop_link_flap(peer)
        ends = " ".join(shlex.quote(end) for end in self.wire_ends(peer))
        script = f"""PID_FILE={shlex.quote(pid_path)}
set_ends() {{
    for end in {ends}; do ip link set dev "$end" "$1" >/dev/null 2>&1 || true; done
}}
cleanup() {{
    set_ends up
    rm -f "$PID_FILE"
}}
trap cleanup EXIT INT TERM
echo $$ > "$PID_FILE"
while true; do
    set_ends down
    sleep {down_time}
    set_ends up
    sleep {up_time}
done
"""
        self._run(
            "sh",
            "-c",
            f"nohup setsid /bin/sh -c {shlex.quote(script)} >/dev/null 2>&1 < /dev/null &",
        )
        self._wait_for_flap_pid(pid_path)

    def start_node_link_flap(
        self, node: str, intf: str, down_time: int, up_time: int
    ) -> str:
        """Flap a direct Containerlab link from the controller namespace.

        Containerlab can connect two node namespaces directly, leaving no host
        veth peer to manipulate. The worker remains on the controller host;
        it enters the node namespace only to change the observable link state.
        """
        target = f"{node}:{intf}"
        pid = self._container_pid(node)
        set_state = f"nsenter -t {pid} -n ip link set dev {shlex.quote(intf)}"
        pid_path = self._flap_pid_path(target)
        self.stop_node_link_flap(node, intf)
        script = f"""PID_FILE={shlex.quote(pid_path)}
cleanup() {{
    {set_state} up >/dev/null 2>&1 || true
    rm -f "$PID_FILE"
}}
trap cleanup EXIT INT TERM
echo $$ > "$PID_FILE"
while true; do
    {set_state} down
    sleep {down_time}
    {set_state} up
    sleep {up_time}
done
"""
        self._run(
            "sh",
            "-c",
            f"nohup setsid /bin/sh -c {shlex.quote(script)} >/dev/null 2>&1 < /dev/null &",
        )
        self._wait_for_flap_pid(pid_path)
        return target

    def link_flap_running(self, peer: str) -> bool:
        pid_path = self._flap_pid_path(peer)
        return (
            self._run(
                "sh",
                "-c",
                f"if [ -r {shlex.quote(pid_path)} ] && "
                f'kill -0 "$(cat {shlex.quote(pid_path)})" 2>/dev/null; then echo running; fi',
            ).strip()
            == "running"
        )

    def stop_link_flap(self, peer: str) -> None:
        pid_path = self._flap_pid_path(peer)
        ends = " ".join(shlex.quote(end) for end in self.wire_ends(peer))
        self._run(
            "sh",
            "-c",
            f"if [ -r {shlex.quote(pid_path)} ]; then "
            f'/bin/kill -KILL -- -"$(cat {shlex.quote(pid_path)})" 2>/dev/null || '
            f'/bin/kill -KILL "$(cat {shlex.quote(pid_path)})" 2>/dev/null || true; fi; '
            f'for end in {ends}; do ip link set dev "$end" up >/dev/null 2>&1 || true; done; '
            f"rm -f {shlex.quote(pid_path)}",
        )

    def stop_node_link_flap(self, node: str, intf: str) -> None:
        target = f"{node}:{intf}"
        pid_path = self._flap_pid_path(target)
        pid = self._container_pid(node)
        self._run(
            "sh",
            "-c",
            f"if [ -r {shlex.quote(pid_path)} ]; then "
            f'/bin/kill -KILL -- -"$(cat {shlex.quote(pid_path)})" 2>/dev/null || '
            f'/bin/kill -KILL "$(cat {shlex.quote(pid_path)})" 2>/dev/null || true; fi; '
            f"nsenter -t {pid} -n ip link set dev {shlex.quote(intf)} up "
            ">/dev/null 2>&1 || true; "
            f"rm -f {shlex.quote(pid_path)}",
        )

    @classmethod
    def cleanup_lab(cls, lab_name: str) -> None:
        """Stop only host-side flap helpers belonging to a lab being torn down."""
        prefix = cls._flap_pid_prefix(lab_name)
        try:
            subprocess.run(
                [
                    "sudo",
                    "-n",
                    "sh",
                    "-c",
                    f"for pid_file in {prefix}*.pid; do "
                    '[ -r "$pid_file" ] || continue; '
                    '/bin/kill -KILL -- -"$(cat "$pid_file")" 2>/dev/null || '
                    '/bin/kill -KILL "$(cat "$pid_file")" 2>/dev/null || true; '
                    'rm -f "$pid_file"; '
                    "done",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=_HOST_CMD_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            print(f"WARNING: stopping host link-flap workers for {lab_name} timed out")

    def _flap_pid_path(self, peer: str) -> str:
        key = hashlib.blake2s(peer.encode(), digest_size=8).hexdigest()
        return f"{self._flap_pid_prefix(self.runtime.lab_name)}{key}.pid"

    def _container_pid(self, node: str) -> int:
        container = self.runtime.get_container(node)
        container.reload()
        pid = int(container.attrs.get("State", {}).get("Pid") or 0)
        if pid <= 0:
            raise RuntimeCapabilityError(f"container for {node!r} is not running")
        return pid

    def _wait_for_flap_pid(self, pid_path: str) -> None:
        self._run(
            "sh",
            "-c",
            f'attempt=0; while [ "$attempt" -lt 20 ]; do '
            f"[ -s {shlex.quote(pid_path)} ] && exit 0; "
            "attempt=$((attempt + 1)); sleep 0.1; "
            "done; exit 1",
        )

    @staticmethod
    def _flap_pid_prefix(lab_name: str) -> str:
        lab_key = hashlib.blake2s(lab_name.encode(), digest_size=8).hexdigest()
        return f"/tmp/nika-link-flap-{lab_key}-"


def host_veth_for(ip_link_output: str, iflink: str, ifindex: str) -> str | None:
    """Return the host veth at ``iflink`` whose peer is ifindex ``ifindex``.

    ``ip_link_output`` is ``ip -o link show`` from the host namespace, with
    rows like ``41: veth1a2b@if40: <...> ... link-netnsid 3`` or ``... link-netns
    clab-lab-node``.
    """
    pattern = re.compile(rf"^{re.escape(iflink)}: ([^:@\s]+)@if(\d+):")
    for row in ip_link_output.splitlines():
        match = pattern.match(row)
        # ``link-netnsid N``, or ``link-netns <name>`` for a namespace that has
        # a name (Containerlab names each node's).
        if match and match.group(2) == ifindex and " link-netns" in row:
            return match.group(1)
    return None
