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


class HostTcController:
    """Apply a qdisc to the host peer of a lab-node interface."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime

    @staticmethod
    def _run(*args: str) -> str:
        try:
            result = subprocess.run(
                ("sudo", "-n", *args), check=False, capture_output=True, text=True
            )
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
        """Resolve ``intf``'s host-side veth by its peer ifindex."""
        container = self.runtime.get_container(node)
        container.reload()
        pid = int(container.attrs.get("State", {}).get("Pid") or 0)
        if pid <= 0:
            raise RuntimeCapabilityError(f"container for {node!r} is not running")
        iflink = self._run(
            "nsenter",
            "-t",
            str(pid),
            "-m",
            "-n",
            "cat",
            f"/sys/class/net/{intf}/iflink",
        ).strip()
        if not iflink.isdecimal():
            raise RuntimeCapabilityError(
                f"could not resolve host peer for {node}:{intf}"
            )
        for row in self._run("ip", "-o", "link", "show").splitlines():
            match = re.match(rf"^{re.escape(iflink)}: ([^:@]+)", row)
            if match:
                return match.group(1)
        raise RuntimeCapabilityError(
            f"host veth ifindex {iflink} for {node}:{intf} is absent"
        )

    def set_netem_corrupt(self, node: str, intf: str, percentage: int) -> str:
        peer = self.peer_name(node, intf)
        self._run(
            "tc",
            "qdisc",
            "replace",
            "dev",
            peer,
            "root",
            "netem",
            "corrupt",
            f"{percentage}%",
        )
        return peer

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
        return peer

    def qdisc(self, peer: str) -> str:
        return self._run("tc", "qdisc", "show", "dev", peer)

    def clear(self, peer: str) -> None:
        # A missing qdisc is the normal result after a lab has been destroyed.
        subprocess.run(
            ["sudo", "-n", "tc", "qdisc", "del", "dev", peer, "root"],
            check=False,
            capture_output=True,
            text=True,
        )

    def start_link_flap(self, peer: str, down_time: int, up_time: int) -> None:
        """Flap a host-side veth without putting fault state in the lab node."""
        pid_path = self._flap_pid_path(peer)
        self.stop_link_flap(peer)
        script = f"""PID_FILE={shlex.quote(pid_path)}
cleanup() {{
    ip link set dev {shlex.quote(peer)} up >/dev/null 2>&1 || true
    rm -f "$PID_FILE"
}}
trap cleanup EXIT INT TERM
echo $$ > "$PID_FILE"
while true; do
    ip link set dev {shlex.quote(peer)} down
    sleep {down_time}
    ip link set dev {shlex.quote(peer)} up
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
        self._run(
            "sh",
            "-c",
            f"if [ -r {shlex.quote(pid_path)} ]; then "
            f'/bin/kill -KILL -- -"$(cat {shlex.quote(pid_path)})" 2>/dev/null || '
            f'/bin/kill -KILL "$(cat {shlex.quote(pid_path)})" 2>/dev/null || true; fi; '
            f"ip link set dev {shlex.quote(peer)} up >/dev/null 2>&1 || true; "
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
        )

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
