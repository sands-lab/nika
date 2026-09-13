"""Size-agnostic L3 Clos topology model for sdn_l3_clos."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

TopoSize = Literal["s", "m", "l"]

# Lab image is nika/onos (see Dockerfile.onos); built on deploy via
# ensure_nika_docker_images for the host architecture.
ONOS_IMAGE = "nika/onos"
SWITCH_IMAGE = "kathara/sdn"
BASE_IMAGE = "nika/base"
NGINX_IMAGE = "nika/nginx"

VIRTUAL_ROUTER_MAC = "02:00:00:00:00:01"
POD = 0
OOB_NETWORK = "172.31.0.0/16"
ONOS_OOB_IP = "172.31.0.100"
FABRIC_MGR_OOB_IP = "172.31.0.101"
ONOS_OF_PORT = 6653
ONOS_REST_PORT = 8181

SIZE_TABLE: dict[TopoSize, tuple[int, int, int]] = {
    # spines, leaves, endpoints_per_leaf (1 web + N-1 clients)
    "s": (2, 4, 2),
    "m": (4, 8, 4),
    "l": (8, 16, 4),
}


def fabric_dimensions(topo_size: TopoSize) -> tuple[int, int, int]:
    if topo_size not in SIZE_TABLE:
        raise ValueError("topo_size should be s, m, or l.")
    return SIZE_TABLE[topo_size]


def dpid_for_leaf(leaf_id: int) -> str:
    """Return 16-hex DPID string for leaf (1-based)."""
    return f"{0x1000 + leaf_id:016x}"


def dpid_for_spine(spine_id: int) -> str:
    return f"{0x2000 + spine_id:016x}"


def device_id(dpid_hex: str) -> str:
    return f"of:{dpid_hex}"


def rack_prefix(leaf_id: int) -> str:
    return f"10.{POD}.{leaf_id}.0/24"


def gateway_ip(leaf_id: int) -> str:
    return f"10.{POD}.{leaf_id}.1"


def endpoint_ip(leaf_id: int, host_index: int) -> str:
    """host_index is 0-based within the leaf; IPs start at .11."""
    return f"10.{POD}.{leaf_id}.{11 + host_index}"


def mac_for_endpoint(leaf_id: int, host_index: int) -> str:
    return f"02:00:{leaf_id:02x}:{host_index:02x}:00:0b"


def mac_for_switch_port(switch_kind: str, switch_id: int, eth_index: int) -> str:
    kind = 0xA0 if switch_kind == "spine" else 0xB0
    return f"02:00:{kind:02x}:{switch_id:02x}:{eth_index:02x}:01"


@dataclass(frozen=True)
class EndpointInfo:
    name: str
    leaf_id: int
    host_index: int
    ip: str
    mac: str
    role: Literal["web", "client"]


@dataclass(frozen=True)
class SwitchPort:
    name: str  # ethN
    peer: str
    mac: str
    role: Literal["host", "spine", "leaf", "oob"]


@dataclass
class ClosFabricModel:
    topo_size: TopoSize
    spine_count: int
    leaf_count: int
    endpoints_per_leaf: int
    spines: list[str] = field(default_factory=list)
    leaves: list[str] = field(default_factory=list)
    endpoints: list[EndpointInfo] = field(default_factory=list)
    # switch_name -> ordered data ports (excludes OOB)
    ports: dict[str, list[SwitchPort]] = field(default_factory=dict)
    # undirected fabric edges (leaf, spine)
    leaf_spine_links: list[tuple[str, str]] = field(default_factory=list)
    web_urls: list[str] = field(default_factory=list)

    @property
    def ecmp_fanout(self) -> int:
        return self.spine_count

    def leaf_id(self, leaf_name: str) -> int:
        return int(leaf_name.split("_", 1)[1])

    def spine_id(self, spine_name: str) -> int:
        return int(spine_name.split("_", 1)[1])

    def endpoints_on_leaf(self, leaf_id: int) -> list[EndpointInfo]:
        return [e for e in self.endpoints if e.leaf_id == leaf_id]

    def web_endpoints(self) -> list[EndpointInfo]:
        return [e for e in self.endpoints if e.role == "web"]

    def client_endpoints(self) -> list[EndpointInfo]:
        return [e for e in self.endpoints if e.role == "client"]

    def port_to_peer(self, switch: str, peer: str) -> SwitchPort | None:
        for port in self.ports.get(switch, []):
            if port.peer == peer:
                return port
        return None

    def expected_device_ids(self) -> list[str]:
        ids = [device_id(dpid_for_spine(i)) for i in range(1, self.spine_count + 1)]
        ids.extend(device_id(dpid_for_leaf(i)) for i in range(1, self.leaf_count + 1))
        return ids

    def expected_leaf_spine_link_count(self) -> int:
        return self.spine_count * self.leaf_count


def build_clos_fabric_model(topo_size: TopoSize) -> ClosFabricModel:
    spines_n, leaves_n, ep_per_leaf = fabric_dimensions(topo_size)
    model = ClosFabricModel(
        topo_size=topo_size,
        spine_count=spines_n,
        leaf_count=leaves_n,
        endpoints_per_leaf=ep_per_leaf,
        spines=[f"spine_{i}" for i in range(1, spines_n + 1)],
        leaves=[f"leaf_{i}" for i in range(1, leaves_n + 1)],
    )

    for leaf_id in range(1, leaves_n + 1):
        for host_index in range(ep_per_leaf):
            role: Literal["web", "client"] = "web" if host_index == 0 else "client"
            name = (
                f"web_{leaf_id}" if role == "web" else f"client_{leaf_id}_{host_index}"
            )
            ep = EndpointInfo(
                name=name,
                leaf_id=leaf_id,
                host_index=host_index,
                ip=endpoint_ip(leaf_id, host_index),
                mac=mac_for_endpoint(leaf_id, host_index),
                role=role,
            )
            model.endpoints.append(ep)
            if role == "web":
                model.web_urls.append(f"http://{ep.ip}/")

    # Port layout (matches l3_clos_topo attachment order):
    # leaf: eth0.. hosts, then ethH.. spines, then OOB last (not in ports)
    # spine: eth0.. leaves, then OOB last
    for leaf_id, leaf in enumerate(model.leaves, start=1):
        ports: list[SwitchPort] = []
        eth = 0
        for ep in model.endpoints_on_leaf(leaf_id):
            ports.append(
                SwitchPort(
                    name=f"eth{eth}",
                    peer=ep.name,
                    mac=mac_for_switch_port("leaf", leaf_id, eth),
                    role="host",
                )
            )
            eth += 1
        for spine_id, spine in enumerate(model.spines, start=1):
            ports.append(
                SwitchPort(
                    name=f"eth{eth}",
                    peer=spine,
                    mac=mac_for_switch_port("leaf", leaf_id, eth),
                    role="spine",
                )
            )
            model.leaf_spine_links.append((leaf, spine))
            eth += 1
        model.ports[leaf] = ports

    for spine_id, spine in enumerate(model.spines, start=1):
        ports = []
        eth = 0
        for leaf_id, leaf in enumerate(model.leaves, start=1):
            ports.append(
                SwitchPort(
                    name=f"eth{eth}",
                    peer=leaf,
                    mac=mac_for_switch_port("spine", spine_id, eth),
                    role="leaf",
                )
            )
            eth += 1
        model.ports[spine] = ports

    return model
