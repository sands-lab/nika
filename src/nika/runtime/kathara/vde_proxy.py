"""Controller-owned VDE fault proxies spliced into a running Kathara link.

Kathara's VDE network plugin backs every collision domain with one
``vde_switch`` hub; a lab interface is a TAP inside its node that the plugin
relays to a hub port. To impair a point-to-point link without touching
either node, a small helper container (outside the lab inventory) takes over
the hub's two ports and bridges them through its own TAPs, where ``tc``
applies the fault (see :mod:`nika.runtime.kathara.vde_splice`). The lab
interfaces are never re-created: ifindex, MAC, counters, qdiscs and carrier
stay as they were, and no protocol session or dataplane restarts. Recovery
hands the ports back to the hub and removes the helper.
"""

from __future__ import annotations

import hashlib
import ipaddress
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

import docker

from nika.runtime.base import RuntimeCapabilityError
from nika.runtime.shared.containers import docker_client, isolated_container_pid
from nika.runtime.shared.processes import process_command, process_source
from nika.net_env.utils.kathara.docker_files.docker_images import nika_image

_SPLICE_SOURCE = (
    (Path(__file__).with_name("vde_splice.py"))
    .read_text()
    .replace(
        "from __future__ import annotations",
        "from __future__ import annotations\n"
        "_PROCESS_HELPERS = {'__name__': 'nika_controller_processes'}\n"
        f"exec({process_source()!r}, _PROCESS_HELPERS)",
    )
)
_READY = "/run/splice.ready"
_LOG = "/run/splice.log"
_PID = "/run/splice.pid"
_READY_TIMEOUT_SEC = 20.0
# Largest TAP MTU: like a cable, the splice must never drop a frame for size.
_SPLICE_MTU = 65521


@dataclass(frozen=True)
class LinkEnd:
    """One lab interface of the spliced link."""

    node: str
    intf: str


@dataclass(frozen=True)
class VdeFaultProxyState:
    """Opaque controller-side reference to an inserted proxy.

    ``eth1`` of the proxy transmits toward ``peer``; ``eth0`` toward
    ``endpoint``.
    """

    key: str
    endpoint: LinkEnd
    peer: LinkEnd
    proxy_id: str


def port_stays_attached(net_env, node: str) -> bool:
    """Whether ``node`` must keep its port: re-creating a RouterOS container
    interface detaches the VM's NIC behind it for good."""
    identities = getattr(net_env, "machine_identities", None) or {}
    identity = identities.get(node)
    return identity is not None and "routeros" in identity.capabilities


def proxy_can_attach(net_env, endpoints) -> bool:
    """Whether a proxy can join a link: at least one endpoint's port must move."""
    return not all(port_stays_attached(net_env, node) for node in endpoints)


class KatharaVdeFaultProxy:
    """Splice a transparent proxy into a point-to-point VDE link."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self._lab = runtime._net_env.lab
        self._client = docker_client()

    def insert(self, node: str, intf: str) -> VdeFaultProxyState:
        existing = self.discover(node, intf)
        if existing is not None:
            return existing
        endpoint, peer = self._link_ends(node, intf)
        hub = self._hub_ctl(endpoint)
        container = self.runtime.get_container(endpoint.node)
        node_pid = isolated_container_pid(container)
        key = self._key(node, intf)
        labels = {
            "nika.fault_proxy": key,
            "nika.lab_name": self.runtime.lab_name,
            "nika.endpoint_node": endpoint.node,
            "nika.endpoint_intf": endpoint.intf,
            "nika.peer_node": peer.node,
            "nika.peer_intf": peer.intf,
        }
        name = f"nika-fp-{key}"
        try:
            # Host PID namespace and privileges let the helper take over the
            # hub's sockets; its own network namespace holds the bridge.
            proxy = self._client.containers.run(
                nika_image("base"),
                command=["/bin/sh", "-c", "exec sleep infinity"],
                privileged=True,
                pid_mode="host",
                network_mode="none",
                detach=True,
                labels=labels,
                name=name,
            )
        except Exception:
            # ``containers.run`` can create the helper and then fail to start
            # it, so look it up by its unique name as well.
            self._remove_container(name)
            raise
        state = VdeFaultProxyState(key, endpoint, peer, proxy.id)
        try:
            self._start_splice(proxy, hub, node_pid, endpoint.intf)
            return state
        except Exception:
            self.remove(state, suppress_errors=True)
            raise

    def set_netem_corrupt(self, state: VdeFaultProxyState, percentage: int) -> None:
        self._set_corrupt(state, ("eth1",), percentage)

    def set_netem_corrupt_bidirectional(
        self, state: VdeFaultProxyState, percentage: int
    ) -> None:
        """Apply corrupt netem on both bridge ports for symmetric RTT impact."""
        self._set_corrupt(state, ("eth0", "eth1"), percentage)

    def _set_corrupt(
        self, state: VdeFaultProxyState, devices: tuple[str, ...], percentage: int
    ) -> None:
        """Corrupt IP frames and drop other frames at the same rate.

        A real link's FCS discards every damaged frame. netem has no FCS: it
        delivers the damaged frame, and only IP-family receivers catch it with
        their own checksums. ARP has none, so a delivered bit flip in an ARP
        reply would poison the peer's neighbor cache and cut the link for a
        reachable-time. Non-IP frames are therefore lost instead of corrupted.
        """
        proxy = self._client.containers.get(state.proxy_id)
        for dev in devices:
            script = " && ".join(
                [
                    f"tc qdisc replace dev {dev} root handle 1: prio bands 2 "
                    f"priomap {' '.join(['0'] * 16)}",
                    f"tc qdisc replace dev {dev} parent 1:1 handle 10: "
                    f"netem loss {percentage}%",
                    f"tc qdisc replace dev {dev} parent 1:2 handle 20: "
                    f"netem corrupt {percentage}%",
                    *(
                        f"tc filter replace dev {dev} parent 1: protocol {proto} "
                        f"prio {prio} u32 match u32 0 0 flowid 1:2"
                        for prio, proto in ((1, "ip"), (2, "ipv6"))
                    ),
                ]
            )
            result = proxy.exec_run(["/bin/sh", "-c", script])
            if result.exit_code:
                raise RuntimeCapabilityError(
                    f"could not configure controller-side VDE proxy on {dev}: "
                    f"{result.output.decode(errors='ignore')}"
                )

    def set_netem_loss(self, state: VdeFaultProxyState, percentage: float) -> None:
        proxy = self._client.containers.get(state.proxy_id)
        result = proxy.exec_run(
            [
                "tc",
                "qdisc",
                "replace",
                "dev",
                "eth1",
                "root",
                "netem",
                "loss",
                f"{percentage:g}%",
            ]
        )
        if result.exit_code:
            raise RuntimeCapabilityError(
                f"could not configure controller-side VDE proxy: {result.output.decode(errors='ignore')}"
            )

    def set_dst_drop(self, state: VdeFaultProxyState, value: str, mask: str) -> None:
        """Drop IPv4 frames toward the peer whose destination matches value/mask.

        *mask* may be non-contiguous (a ternary match), which ``u32`` matches
        on the destination address word of the IPv4 header.
        """
        val, msk = _ipv4_word(value), _ipv4_word(mask)
        proxy = self._client.containers.get(state.proxy_id)
        result = proxy.exec_run(
            [
                "/bin/sh",
                "-c",
                "tc qdisc replace dev eth1 root handle 1: prio && "
                "tc filter replace dev eth1 parent 1: protocol ip prio 1 u32 "
                f"match u32 0x{val & msk:08x} 0x{msk:08x} at 16 action drop",
            ]
        )
        if result.exit_code:
            raise RuntimeCapabilityError(
                f"could not configure controller-side VDE proxy filter: "
                f"{result.output.decode(errors='ignore')}"
            )

    def dst_drop_configured(
        self, state: VdeFaultProxyState, value: str, mask: str
    ) -> bool:
        val, msk = _ipv4_word(value), _ipv4_word(mask)
        proxy = self._client.containers.get(state.proxy_id)
        result = proxy.exec_run(["tc", "filter", "show", "dev", "eth1"])
        return (
            result.exit_code == 0
            and f"match {val & msk:08x}/{msk:08x} at 16".encode() in result.output
        )

    def qdisc_show(self, state: VdeFaultProxyState) -> str:
        """``tc qdisc show`` of the proxy port toward the peer."""
        proxy = self._client.containers.get(state.proxy_id)
        result = proxy.exec_run(["tc", "qdisc", "show", "dev", "eth1"])
        return result.output.decode(errors="ignore") if result.exit_code == 0 else ""

    def netem_configured(self, state: VdeFaultProxyState) -> bool:
        proxy = self._client.containers.get(state.proxy_id)
        result = proxy.exec_run(["tc", "qdisc", "show", "dev", "eth1"])
        return result.exit_code == 0 and b"netem" in result.output.lower()

    def netem_corrupt_configured(self, state: VdeFaultProxyState) -> bool:
        proxy = self._client.containers.get(state.proxy_id)
        result = proxy.exec_run(["tc", "qdisc", "show", "dev", "eth1"])
        output = result.output.lower()
        return result.exit_code == 0 and b"netem" in output and b"corrupt" in output

    def set_tbf(
        self,
        state: VdeFaultProxyState,
        rate: str,
        burst: str,
        limit: str,
    ) -> None:
        proxy = self._client.containers.get(state.proxy_id)
        result = proxy.exec_run(
            [
                "tc",
                "qdisc",
                "replace",
                "dev",
                "eth1",
                "root",
                "tbf",
                "rate",
                rate,
                "burst",
                burst,
                "limit",
                limit,
            ]
        )
        if result.exit_code:
            raise RuntimeCapabilityError(
                f"could not configure controller-side VDE proxy TBF: "
                f"{result.output.decode(errors='ignore')}"
            )

    def tbf_configured(self, state: VdeFaultProxyState) -> bool:
        proxy = self._client.containers.get(state.proxy_id)
        result = proxy.exec_run(["tc", "qdisc", "show", "dev", "eth1"])
        return result.exit_code == 0 and b"tbf" in result.output.lower()

    def tbf_overlimits(self, state: VdeFaultProxyState) -> int | None:
        """Return summed overlimits/dropped counters from the proxy TBF qdisc."""
        proxy = self._client.containers.get(state.proxy_id)
        result = proxy.exec_run(["tc", "-s", "qdisc", "show", "dev", "eth1"])
        if result.exit_code != 0:
            return None
        output = result.output.decode(errors="ignore")
        if "tbf" not in output.lower():
            return None
        total = 0
        found = False
        for line in output.splitlines():
            lower = line.lower()
            if "overlimits" in lower or "dropped" in lower:
                found = True
                for token in line.replace(",", " ").split():
                    if token.isdigit():
                        total += int(token)
        return total if found else 0

    def start_link_flap(
        self, state: VdeFaultProxyState, down_time: int, up_time: int
    ) -> None:
        """Flap a proxy port while keeping the controller implementation hidden."""
        self.stop_link_flap(state)
        pid_path = self._flap_pid_path(state)
        script = f"""PID_FILE={shlex.quote(pid_path)}
cleanup() {{
    ip link set dev eth1 up >/dev/null 2>&1 || true
    rm -f "$PID_FILE"
}}
trap cleanup EXIT
trap 'exit 0' INT TERM
{shlex.join(process_command("record", pid_path))} $$ || exit 1
while true; do
    ip link set dev eth1 down
    sleep {down_time}
    ip link set dev eth1 up
    sleep {up_time}
done
"""
        proxy = self._client.containers.get(state.proxy_id)
        result = proxy.exec_run(
            [
                "/bin/sh",
                "-c",
                f"nohup setsid /bin/sh -c {shlex.quote(script)} >/dev/null 2>&1 < /dev/null &",
            ]
        )
        if result.exit_code:
            raise RuntimeCapabilityError("could not start controller-side link flap")
        self._wait_for_flap_pid(proxy, pid_path)

    def link_flap_running(self, state: VdeFaultProxyState) -> bool:
        pid_path = self._flap_pid_path(state)
        proxy = self._client.containers.get(state.proxy_id)
        result = proxy.exec_run(process_command("running", pid_path))
        return result.exit_code == 0 and result.output.strip() == b"running"

    def stop_link_flap(self, state: VdeFaultProxyState) -> None:
        pid_path = self._flap_pid_path(state)
        try:
            proxy = self._client.containers.get(state.proxy_id)
        except docker.errors.NotFound:
            return
        result = proxy.exec_run(process_command("stop", pid_path))
        if result.exit_code:
            raise RuntimeCapabilityError(
                "could not stop verified controller-side flap worker"
            )
        proxy.exec_run(["ip", "link", "set", "dev", "eth1", "up"])

    def remove(
        self, state: VdeFaultProxyState, *, suppress_errors: bool = False
    ) -> None:
        try:
            proxy = self._client.containers.get(state.proxy_id)
        except docker.errors.NotFound:
            return
        try:
            self.stop_link_flap(state)
            self._stop_splice(proxy)
        except Exception:
            if not suppress_errors:
                raise
        finally:
            self._remove_container(state.proxy_id)

    def discover(self, node: str, intf: str) -> VdeFaultProxyState | None:
        key = self._key(node, intf)
        containers = self._client.containers.list(
            all=True, filters={"label": f"nika.fault_proxy={key}"}
        )
        if not containers:
            return None
        labels = containers[0].labels or {}
        try:
            endpoint = LinkEnd(
                labels["nika.endpoint_node"], labels["nika.endpoint_intf"]
            )
            peer = LinkEnd(labels["nika.peer_node"], labels["nika.peer_intf"])
        except KeyError:
            return None
        return VdeFaultProxyState(key, endpoint, peer, containers[0].id)

    @classmethod
    def cleanup_lab(cls, lab_name: str) -> None:
        """Remove only proxy helpers labelled for a lab being destroyed."""
        client = docker_client()
        label = f"nika.lab_name={lab_name}"
        for container in client.containers.list(all=True, filters={"label": label}):
            try:
                cls._stop_splice(container)
            except Exception:
                # Kathara kills the paused hub with the lab anyway.
                pass
            container.remove(force=True)

    def _start_splice(self, proxy, hub: str, node_pid: int, intf: str) -> None:
        args = " ".join(
            shlex.quote(str(item))
            for item in (_SPLICE_SOURCE, hub, node_pid, intf, _SPLICE_MTU, _READY)
        )
        proxy.exec_run(
            [
                "/bin/sh",
                "-c",
                f"{shlex.join(process_command('record', _PID))} $$ && exec python3 -c {args} >{_LOG} 2>&1",
            ],
            detach=True,
        )
        started = time.monotonic()
        deadline = started + _READY_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if proxy.exec_run(["test", "-s", _READY]).exit_code == 0:
                return
            alive = proxy.exec_run(process_command("running", _PID))
            if (
                alive.exit_code or alive.output.strip() != b"running"
            ) and time.monotonic() > started + 2:
                break
            time.sleep(0.2)
        log = proxy.exec_run(["cat", _LOG]).output.decode(errors="ignore")
        raise RuntimeCapabilityError(f"could not splice VDE link {hub}: {log.strip()}")

    @staticmethod
    def _stop_splice(proxy) -> None:
        """Hand the ports back to the hub and confirm it forwards again."""
        script = (
            process_source()
            + f"""
import time
path = {_READY!r}
if not Path(path).is_file():
    raise SystemExit(0)
record = read_identity(path)
if not isinstance(record, dict):
    raise RuntimeError("Refusing unverified VDE relay and hub identities")
stop_owned(record.get("relay"))
hub = record.get("hub")
fd = owned_pidfd(hub)
if fd is not None:
    try:
        send_pidfd(fd, signal.SIGCONT)
        for _ in range(50):
            try:
                state = Path(f"/proc/{{hub[0]}}/status").read_text()
            except FileNotFoundError:
                break
            if not any(line.startswith("State:") and "stopped" in line for line in state.splitlines()):
                break
            time.sleep(0.02)
        else:
            raise RuntimeError("VDE hub did not resume after the splice")
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)
Path(path).unlink(missing_ok=True)
"""
        )
        result = proxy.exec_run(["python3", "-c", script])
        if result.exit_code:
            raise RuntimeCapabilityError(
                f"VDE hub did not resume after the splice: {result.output.decode(errors='ignore')}"
            )

    def _remove_container(self, ref: str) -> None:
        try:
            self._client.containers.get(ref).remove(force=True)
        except docker.errors.NotFound:
            pass

    def _link_ends(self, node: str, intf: str) -> tuple[LinkEnd, LinkEnd]:
        try:
            number = int(intf.removeprefix("eth"))
            link = self._lab.machines[node].interfaces[number].link
            peers = [
                machine.name
                for machine in link.machines.values()
                if machine.name != node
            ]
            if len(peers) != 1:
                raise ValueError("link is not point-to-point")
            peer_interface = next(
                item
                for item in self._lab.machines[peers[0]].interfaces.values()
                if item.link is link
            )
        except (KeyError, StopIteration, ValueError) as exc:
            raise RuntimeCapabilityError(
                f"VDE fault proxy requires a point-to-point link at {node}:{intf}"
            ) from exc
        return LinkEnd(node, intf), LinkEnd(peers[0], f"eth{peer_interface.num}")

    def _hub_ctl(self, end: LinkEnd) -> str:
        """Control path suffix of the plugin hub serving ``end``."""
        container = self.runtime.get_container(end.node)
        container.reload()
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        number = end.intf.removeprefix("eth")
        for attachment in networks.values():
            options = attachment.get("DriverOpts") or {}
            if options.get("kathara.iface") == number:
                # katharanp names each hub after the first 12 hex digits of
                # its Docker network ID.
                return f"kt-{attachment['NetworkID'][:12]}/ctl"
        raise RuntimeCapabilityError(
            f"could not resolve deployed VDE LAN for {end.node}:{end.intf}"
        )

    def _key(self, node: str, intf: str) -> str:
        return hashlib.blake2s(
            f"{self.runtime.lab_name}:{node}:{intf}".encode(),
            digest_size=8,
        ).hexdigest()

    @staticmethod
    def _flap_pid_path(state: VdeFaultProxyState) -> str:
        return f"/tmp/nika-link-flap-{state.key}.pid"

    @staticmethod
    def _wait_for_flap_pid(proxy, pid_path: str) -> None:
        result = proxy.exec_run(
            [
                "/bin/sh",
                "-c",
                f'attempt=0; while [ "$attempt" -lt 20 ]; do '
                f"[ -s {shlex.quote(pid_path)} ] && exit 0; "
                "attempt=$((attempt + 1)); sleep 0.1; "
                "done; exit 1",
            ]
        )
        if result.exit_code:
            raise RuntimeCapabilityError(
                "controller-side link flap worker did not start"
            )


def _ipv4_word(address: str) -> int:
    return int(ipaddress.IPv4Address(address))
