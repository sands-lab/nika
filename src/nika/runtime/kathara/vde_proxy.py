"""Controller-owned, dynamically inserted VDE fault proxies.

Kathara VDE collision domains do not have a host veth peer.  To inject a
link fault without placing ``tc`` configuration in a lab node, this module
temporarily replaces one point-to-point VDE LAN with two VDE LANs joined by a
small transparent bridge container.  The container and its networks are not
part of the lab inventory and are removed during recovery or lab teardown.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import time
from dataclasses import dataclass

import docker
from docker.types import IPAMConfig

from nika.runtime.base import RuntimeCapabilityError


@dataclass(frozen=True)
class _Endpoint:
    node: str
    intf: str
    number: int
    mac: str
    mtu: int
    addresses: tuple[str, ...]
    routes: tuple[dict, ...] = ()


@dataclass(frozen=True)
class VdeFaultProxyState:
    """Opaque controller-side reference to an inserted proxy."""

    key: str
    endpoint: _Endpoint
    peer: _Endpoint
    original_network_id: str
    lan_a_id: str
    lan_b_id: str
    proxy_id: str


class KatharaVdeFaultProxy:
    """Insert and remove a transparent proxy for a point-to-point VDE link."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self._lab = runtime._net_env.lab
        self._client = docker.from_env()

    def insert(self, node: str, intf: str) -> VdeFaultProxyState:
        endpoint, peer, original = self._endpoints(node, intf)
        key = self._key(endpoint)
        existing = self.discover(node, intf)
        if existing is not None:
            return existing

        labels = {
            "nika.fault_proxy": key,
            "nika.lab_name": self.runtime.lab_name,
            "nika.original_network": original.id,
            "nika.endpoint_node": endpoint.node,
            "nika.peer_node": peer.node,
        }
        lan_a = self._create_lan(f"nika-fp-{key}-a", labels, original)
        lan_b = self._create_lan(f"nika-fp-{key}-b", labels, original)
        proxy = self._client.containers.run(
            "nika/base",
            command=["/bin/sh", "-c", "exec sleep infinity"],
            cap_add=["NET_ADMIN"],
            detach=True,
            labels=labels,
            name=f"nika-fp-{key}",
        )
        state = VdeFaultProxyState(
            key=key,
            endpoint=endpoint,
            peer=peer,
            original_network_id=original.id,
            lan_a_id=lan_a.id,
            lan_b_id=lan_b.id,
            proxy_id=proxy.id,
        )
        try:
            # Docker's ``none`` network mode forbids connecting an additional
            # network.  Start on the ordinary bridge, then detach it before
            # connecting the two controller-only VDE LANs.
            self._client.networks.get("bridge").disconnect(proxy, force=True)
            self._connect(proxy, lan_a, 0, f"fp-{key}-a")
            self._connect(proxy, lan_b, 1, f"fp-{key}-b")
            self._normalise_proxy_interfaces(proxy)
            self._bridge(proxy)
            original.disconnect(self.runtime.get_container(endpoint.node), force=True)
            original.disconnect(self.runtime.get_container(peer.node), force=True)
            self._connect(
                self.runtime.get_container(endpoint.node),
                lan_a,
                endpoint.number,
                f"fp-{key}-a",
                endpoint.mac,
            )
            self._restore_endpoint(endpoint)
            self._connect(
                self.runtime.get_container(peer.node),
                lan_b,
                peer.number,
                f"fp-{key}-b",
                peer.mac,
            )
            self._restore_endpoint(peer)
            self.verify_identity(state)
            return state
        except Exception:
            self.remove(state, suppress_errors=True)
            raise

    def set_netem_corrupt(self, state: VdeFaultProxyState, percentage: int) -> None:
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
                "corrupt",
                f"{percentage}%",
            ]
        )
        if result.exit_code:
            raise RuntimeCapabilityError(
                f"could not configure controller-side VDE proxy: {result.output.decode(errors='ignore')}"
            )

    def set_netem_loss(self, state: VdeFaultProxyState, percentage: int) -> None:
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
                f"{percentage}%",
            ]
        )
        if result.exit_code:
            raise RuntimeCapabilityError(
                f"could not configure controller-side VDE proxy: {result.output.decode(errors='ignore')}"
            )

    def netem_configured(self, state: VdeFaultProxyState) -> bool:
        proxy = self._client.containers.get(state.proxy_id)
        result = proxy.exec_run(["tc", "qdisc", "show", "dev", "eth1"])
        return result.exit_code == 0 and b"netem" in result.output.lower()

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
trap cleanup EXIT INT TERM
echo $$ > "$PID_FILE"
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
        result = proxy.exec_run(
            [
                "/bin/sh",
                "-c",
                f"if [ -r {shlex.quote(pid_path)} ] && "
                f'kill -0 "$(cat {shlex.quote(pid_path)})" 2>/dev/null; then echo running; fi',
            ]
        )
        return result.exit_code == 0 and result.output.strip() == b"running"

    def stop_link_flap(self, state: VdeFaultProxyState) -> None:
        pid_path = self._flap_pid_path(state)
        try:
            proxy = self._client.containers.get(state.proxy_id)
        except docker.errors.NotFound:
            return
        proxy.exec_run(
            [
                "/bin/sh",
                "-c",
                f"if [ -r {shlex.quote(pid_path)} ]; then "
                f'/bin/kill -KILL -- -"$(cat {shlex.quote(pid_path)})" 2>/dev/null || '
                f'/bin/kill -KILL "$(cat {shlex.quote(pid_path)})" 2>/dev/null || true; fi; '
                "ip link set dev eth1 up >/dev/null 2>&1 || true; "
                f"rm -f {shlex.quote(pid_path)}",
            ]
        )

    def verify_identity(self, state: VdeFaultProxyState) -> None:
        """Ensure reattachment retained the externally observable L2 identity."""
        for endpoint in (state.endpoint, state.peer):
            info = self._link_info(endpoint.node, endpoint.intf)
            if (
                info.get("ifname") != endpoint.intf
                or info.get("address", "").lower() != endpoint.mac.lower()
            ):
                raise RuntimeCapabilityError(
                    f"VDE proxy changed interface identity for {endpoint.node}:{endpoint.intf}"
                )

    def remove(
        self, state: VdeFaultProxyState, *, suppress_errors: bool = False
    ) -> None:
        try:
            self.stop_link_flap(state)
            original = self._client.networks.get(state.original_network_id)
            for endpoint, lan_id in (
                (state.endpoint, state.lan_a_id),
                (state.peer, state.lan_b_id),
            ):
                container = self.runtime.get_container(endpoint.node)
                try:
                    self._client.networks.get(lan_id).disconnect(container, force=True)
                except docker.errors.NotFound:
                    pass
                self._connect(
                    container, original, endpoint.number, "restored", endpoint.mac
                )
                self._restore_endpoint(endpoint)
            self.verify_identity(state)
        except Exception:
            if not suppress_errors:
                raise
        finally:
            for resource_id, kind in (
                (state.proxy_id, "container"),
                (state.lan_a_id, "network"),
                (state.lan_b_id, "network"),
            ):
                try:
                    if kind == "container":
                        self._client.containers.get(resource_id).remove(force=True)
                    else:
                        self._client.networks.get(resource_id).remove()
                except docker.errors.NotFound:
                    pass

    def discover(self, node: str, intf: str) -> VdeFaultProxyState | None:
        endpoint, peer, original = self._endpoints(node, intf)
        key = self._key(endpoint)
        containers = self._client.containers.list(
            all=True, filters={"label": f"nika.fault_proxy={key}"}
        )
        if not containers:
            return None
        networks = self._client.networks.list(
            filters={"label": f"nika.fault_proxy={key}"}
        )
        if len(networks) != 2:
            return None
        lan_a, lan_b = sorted(networks, key=lambda item: item.name)
        original_id = containers[0].labels.get("nika.original_network")
        if not original_id:
            return None
        return VdeFaultProxyState(
            key, endpoint, peer, original_id, lan_a.id, lan_b.id, containers[0].id
        )

    @classmethod
    def cleanup_lab(cls, lab_name: str) -> None:
        """Remove only proxy resources labelled for a lab being destroyed."""
        client = docker.from_env()
        label = f"nika.lab_name={lab_name}"
        for container in client.containers.list(all=True, filters={"label": label}):
            container.remove(force=True)
        for network in client.networks.list(filters={"label": label}):
            try:
                # Detach only test/lab endpoints from the dynamic LAN.  There
                # is no need to restore them during full lab teardown, but
                # leaving them attached makes Kathara mistake the LAN for a
                # declared collision domain while it inventories the lab.
                network.reload()
                for attached in list(network.containers):
                    network.disconnect(attached, force=True)
                network.remove()
            except docker.errors.APIError:
                # A lab endpoint may still be attached while teardown is underway.
                pass

    def _endpoints(self, node: str, intf: str):
        try:
            number = int(intf.removeprefix("eth"))
            interface = self._lab.machines[node].interfaces[number]
            link = interface.link
            peers = [
                machine.name
                for machine in link.machines.values()
                if machine.name != node
            ]
            if len(peers) != 1:
                raise ValueError("link is not point-to-point")
            peer_name = peers[0]
            peer_interface = next(
                item
                for item in self._lab.machines[peer_name].interfaces.values()
                if item.link is link
            )
        except (KeyError, StopIteration, ValueError) as exc:
            raise RuntimeCapabilityError(
                f"dynamic VDE proxy requires a point-to-point link at {node}:{intf}"
            ) from exc
        original = self._network_for_interface(node, number)
        return (
            self._capture_endpoint(node, intf, number),
            self._capture_endpoint(
                peer_name, f"eth{peer_interface.num}", peer_interface.num
            ),
            original,
        )

    def _create_lan(self, name: str, labels: dict[str, str], original):
        return self._client.networks.create(
            name=name,
            driver=original.attrs["Driver"],
            ipam=IPAMConfig(driver="null"),
            labels=labels,
        )

    def _network_for_interface(self, node: str, number: int):
        container = self.runtime.get_container(node)
        container.reload()
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        for network_name, attachment in networks.items():
            options = attachment.get("DriverOpts") or {}
            if options.get("kathara.iface") == str(number):
                return self._client.networks.get(
                    attachment["NetworkID"] or network_name
                )
        raise RuntimeCapabilityError(
            f"could not resolve deployed VDE LAN for {node}:eth{number}"
        )

    @staticmethod
    def _connect(
        container, network, number: int, link_name: str, mac: str | None = None
    ) -> None:
        options = {"kathara.iface": str(number), "kathara.link": link_name}
        if mac:
            options["kathara.mac_addr"] = mac
        network.connect(container, driver_opt=options)

    @staticmethod
    def _bridge(proxy) -> None:
        result = proxy.exec_run(
            [
                "/bin/sh",
                "-c",
                "ip link add br0 type bridge stp_state 0 forward_delay 0 && ip link set eth0 master br0 && ip link set eth1 master br0 && ip link set eth0 up && ip link set eth1 up && ip link set br0 up",
            ]
        )
        if result.exit_code:
            raise RuntimeCapabilityError(
                f"could not start VDE proxy bridge: {result.output.decode(errors='ignore')}"
            )

    @staticmethod
    def _normalise_proxy_interfaces(proxy) -> None:
        result = proxy.exec_run(["ip", "-j", "link"])
        if result.exit_code:
            raise RuntimeCapabilityError("could not inspect VDE proxy interfaces")
        interfaces = [
            item["ifname"]
            for item in json.loads(result.output)
            if item["ifname"] != "lo"
        ]
        if len(interfaces) != 2:
            raise RuntimeCapabilityError("VDE proxy did not receive two LAN interfaces")
        for current, expected in zip(sorted(interfaces), ("eth0", "eth1"), strict=True):
            if current != expected:
                result = proxy.exec_run(
                    ["ip", "link", "set", "dev", current, "name", expected]
                )
                if result.exit_code:
                    raise RuntimeCapabilityError(
                        "could not normalize VDE proxy interface names"
                    )

    def _capture_endpoint(self, node: str, intf: str, number: int) -> _Endpoint:
        info = self._link_info(node, intf)
        return _Endpoint(
            node,
            intf,
            number,
            info["address"],
            info["mtu"],
            self._addresses(node, intf),
            self._routes(node, intf),
        )

    def _routes(self, node: str, intf: str) -> tuple[dict, ...]:
        """Capture non-kernel routes that would be dropped when the NIC is moved."""
        try:
            rows = json.loads(self.runtime.exec(node, "ip -j route show"))
        except json.JSONDecodeError:
            return ()
        kept: list[dict] = []
        for row in rows:
            if row.get("dev") != intf:
                continue
            # Address install recreates proto-kernel link routes.
            if row.get("protocol") == "kernel" and row.get("scope") == "link":
                continue
            kept.append(row)
        return tuple(kept)

    @staticmethod
    def _route_replace_cmd(row: dict, intf: str) -> str:
        dst = row.get("dst") or "default"
        parts = ["ip", "route", "replace", str(dst)]
        gateway = row.get("gateway")
        if gateway:
            parts.extend(["via", str(gateway)])
        parts.extend(["dev", intf])
        metric = row.get("metric")
        if metric is not None:
            parts.extend(["metric", str(metric)])
        prefsrc = row.get("prefsrc")
        if prefsrc:
            parts.extend(["src", str(prefsrc)])
        return " ".join(parts)

    def _restore_endpoint(self, endpoint: _Endpoint) -> None:
        """Rename the new VDE NIC and restore the interface's L3 state."""
        for _ in range(10):
            try:
                links = json.loads(self.runtime.exec(endpoint.node, "ip -j link"))
            except json.JSONDecodeError:
                time.sleep(0.5)
                continue
            matching = next(
                (
                    item
                    for item in links
                    if item.get("address", "").lower() == endpoint.mac.lower()
                ),
                None,
            )
            if matching is None:
                time.sleep(0.5)
                continue
            current = matching["ifname"]
            if current != endpoint.intf:
                self.runtime.exec(
                    endpoint.node,
                    f"ip link set dev {current} name {endpoint.intf}",
                )
            self.runtime.exec(
                endpoint.node, f"ip link set dev {endpoint.intf} mtu {endpoint.mtu}"
            )
            self.runtime.exec(endpoint.node, f"ip link set dev {endpoint.intf} up")
            for address in endpoint.addresses:
                self.runtime.exec(
                    endpoint.node, f"ip address replace {address} dev {endpoint.intf}"
                )
            for row in endpoint.routes:
                self.runtime.exec(
                    endpoint.node, self._route_replace_cmd(row, endpoint.intf)
                )
            return
        raise RuntimeCapabilityError(
            f"VDE proxy did not attach {endpoint.node}:{endpoint.intf}"
        )

    def _addresses(self, node: str, intf: str) -> tuple[str, ...]:
        output = self.runtime.exec(node, f"ip -j address show dev {intf}")
        try:
            addresses = json.loads(output)[0].get("addr_info", [])
        except (IndexError, json.JSONDecodeError) as exc:
            raise RuntimeCapabilityError(
                f"could not inspect addresses for {node}:{intf}"
            ) from exc
        return tuple(
            f"{item['local']}/{item['prefixlen']}"
            for item in addresses
            if item.get("family") in {"inet", "inet6"} and item.get("local")
        )

    def _link_info(self, node: str, intf: str) -> dict:
        for _ in range(10):
            output = self.runtime.exec(node, f"ip -j link show dev {intf}")
            try:
                return json.loads(output)[0]
            except (IndexError, json.JSONDecodeError):
                time.sleep(0.5)
        raise RuntimeCapabilityError(f"could not inspect {node}:{intf}")

    def _key(self, endpoint: _Endpoint) -> str:
        return hashlib.blake2s(
            f"{self.runtime.lab_name}:{endpoint.node}:{endpoint.intf}".encode(),
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
