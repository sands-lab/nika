"""Topology and addressing model for the P4 gateway benchmark fabric."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

TopoSize = Literal["s", "m", "l"]

SWITCH_IMAGE = "kathara/p4"
BASE_IMAGE = "nika/base"
NGINX_IMAGE = "nika/nginx"
CONTROLLER_IMAGE = "nika/fabric-controller"
COLLECTOR_IMAGE = "nika/base"

P4RUNTIME_PORT = 9559
VIRTUAL_ROUTER_MAC = "02:00:00:00:00:01"
OOB_PREFIX = "172.30.0"
TELEMETRY_PREFIX = "172.29.0"
CONTROLLER_OOB_IP = f"{OOB_PREFIX}.250"
COLLECTOR_TELEMETRY_IP = f"{TELEMETRY_PREFIX}.250"
PIPELINE_NAME = "gateway"
VIP_IP = "20.0.0.1"
VIP_PORT = 80
CONN_TABLE_CAPACITY = 256
CONNECTION_LEARNING_DELAY_MS = 5

# gateways, spines, leaves, external clients, HTTP services
SIZE_TABLE: dict[TopoSize, tuple[int, int, int, int, int]] = {
    "s": (2, 2, 2, 2, 4),
    "m": (4, 4, 4, 4, 8),
    "l": (8, 8, 8, 8, 16),
}


def gateway_dimensions(topo_size: TopoSize) -> tuple[int, int, int, int, int]:
    try:
        return SIZE_TABLE[topo_size]
    except KeyError as exc:
        raise ValueError("topo_size should be s, m, or l.") from exc


def client_ip(index: int) -> str:
    return f"192.0.2.{10 + index}"


def service_ip(leaf_id: int, service_index: int) -> str:
    return f"10.0.{leaf_id}.{10 + service_index}"


def service_prefix(leaf_id: int) -> str:
    return f"10.0.{leaf_id}.0/24"


def service_gateway(leaf_id: int) -> str:
    return f"10.0.{leaf_id}.1"


def switch_device_id(role: str, index: int) -> int:
    bases = {"gateway": 0, "spine": 100, "leaf": 200}
    return bases[role] + index


def switch_oob_ip(role: str, index: int) -> str:
    bases = {"gateway": 10, "spine": 80, "leaf": 150}
    return f"{OOB_PREFIX}.{bases[role] + index}"


def switch_telemetry_ip(role: str, index: int) -> str:
    bases = {"gateway": 10, "spine": 80, "leaf": 150}
    return f"{TELEMETRY_PREFIX}.{bases[role] + index}"


def endpoint_mac(kind: str, major: int, minor: int = 0) -> str:
    prefix = 0xC0 if kind == "client" else 0xD0
    return f"02:00:{prefix:02x}:{major:02x}:{minor:02x}:0b"


def port_mac(role: str, switch_id: int, eth_index: int) -> str:
    prefix = {"gateway": 0xA0, "spine": 0xB0, "leaf": 0xC0}[role]
    return f"02:00:{prefix:02x}:{switch_id:02x}:{eth_index:02x}:01"


@dataclass(frozen=True)
class Endpoint:
    name: str
    role: Literal["external_client", "http_service"]
    ip: str
    mac: str
    attached_switch: str


@dataclass(frozen=True)
class SwitchPort:
    name: str
    peer: str
    role: Literal["client", "gateway", "spine", "leaf", "service"]
    mac: str
    bmv2_port: int


@dataclass(frozen=True)
class SwitchInfo:
    name: str
    role: Literal["gateway", "spine", "leaf"]
    index: int
    device_id: int
    oob_ip: str
    telemetry_ip: str


@dataclass
class GatewayFabricModel:
    topo_size: TopoSize
    gateway_count: int
    spine_count: int
    leaf_count: int
    client_count: int
    service_count: int
    gateways: list[str] = field(default_factory=list)
    spines: list[str] = field(default_factory=list)
    leaves: list[str] = field(default_factory=list)
    clients: list[Endpoint] = field(default_factory=list)
    services: list[Endpoint] = field(default_factory=list)
    switch_info: dict[str, SwitchInfo] = field(default_factory=dict)
    ports: dict[str, list[SwitchPort]] = field(default_factory=dict)
    gateway_spine_links: list[tuple[str, str]] = field(default_factory=list)
    spine_leaf_links: list[tuple[str, str]] = field(default_factory=list)

    @property
    def web_urls(self) -> list[str]:
        return [f"http://{service.ip}/" for service in self.services]

    @property
    def vip_url(self) -> str:
        return f"http://{VIP_IP}:{VIP_PORT}/"

    @property
    def backend_pool(self) -> list[Endpoint]:
        """The two deterministic backends used by the L4 gateway workload."""
        return self.services[:2]

    @property
    def endpoints(self) -> list[Endpoint]:
        return self.clients + self.services

    def fabric_switches(self) -> list[str]:
        return self.gateways + self.spines + self.leaves

    def port_to_peer(self, switch: str, peer: str) -> SwitchPort | None:
        return next(
            (port for port in self.ports.get(switch, []) if port.peer == peer), None
        )

    def services_on_leaf(self, leaf: str) -> list[Endpoint]:
        return [item for item in self.services if item.attached_switch == leaf]

    def client_on_gateway(self, gateway: str) -> Endpoint:
        return next(item for item in self.clients if item.attached_switch == gateway)

    def expected_gateway_spine_link_count(self) -> int:
        return self.gateway_count * self.spine_count

    def expected_spine_leaf_link_count(self) -> int:
        return self.spine_count * self.leaf_count


def build_gateway_fabric_model(topo_size: TopoSize) -> GatewayFabricModel:
    ng, ns, nl, nc, nsvc = gateway_dimensions(topo_size)
    model = GatewayFabricModel(
        topo_size=topo_size,
        gateway_count=ng,
        spine_count=ns,
        leaf_count=nl,
        client_count=nc,
        service_count=nsvc,
        gateways=[f"gateway_{i}" for i in range(1, ng + 1)],
        spines=[f"spine_{i}" for i in range(1, ns + 1)],
        leaves=[f"leaf_{i}" for i in range(1, nl + 1)],
    )
    for role, names in (
        ("gateway", model.gateways),
        ("spine", model.spines),
        ("leaf", model.leaves),
    ):
        for index, name in enumerate(names, start=1):
            model.switch_info[name] = SwitchInfo(
                name=name,
                role=role,  # type: ignore[arg-type]
                index=index,
                device_id=switch_device_id(role, index),
                oob_ip=switch_oob_ip(role, index),
                telemetry_ip=switch_telemetry_ip(role, index),
            )

    for index, gateway in enumerate(model.gateways, start=1):
        model.clients.append(
            Endpoint(
                f"client_{index}",
                "external_client",
                client_ip(index),
                endpoint_mac("client", index),
                gateway,
            )
        )
    services_per_leaf = nsvc // nl
    for leaf_id, leaf in enumerate(model.leaves, start=1):
        for service_index in range(1, services_per_leaf + 1):
            model.services.append(
                Endpoint(
                    f"service_{leaf_id}_{service_index}",
                    "http_service",
                    service_ip(leaf_id, service_index),
                    endpoint_mac("service", leaf_id, service_index),
                    leaf,
                )
            )

    def add_port(switch: str, peer: str, peer_role: str) -> None:
        info = model.switch_info[switch]
        eth_index = len(model.ports.setdefault(switch, []))
        model.ports[switch].append(
            SwitchPort(
                f"eth{eth_index}",
                peer,
                peer_role,
                port_mac(info.role, info.index, eth_index),
                eth_index + 1,
            )
        )  # type: ignore[arg-type]

    for client in model.clients:
        add_port(client.attached_switch, client.name, "client")
    for gateway in model.gateways:
        for spine in model.spines:
            add_port(gateway, spine, "spine")
            add_port(spine, gateway, "gateway")
            model.gateway_spine_links.append((gateway, spine))
    for spine in model.spines:
        for leaf in model.leaves:
            add_port(spine, leaf, "leaf")
            add_port(leaf, spine, "spine")
            model.spine_leaf_links.append((spine, leaf))
    for service in model.services:
        add_port(service.attached_switch, service.name, "service")
    return model
