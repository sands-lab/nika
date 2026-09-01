"""P4Runtime intent for the gateway-spine-leaf fabric."""

from __future__ import annotations

from typing import Any

from nika.net_env.p4_dc_gateway.topology_model import (
    CONN_TABLE_CAPACITY,
    CONNECTION_LEARNING_DELAY_MS,
    P4RUNTIME_PORT,
    PIPELINE_NAME,
    VIRTUAL_ROUTER_MAC,
    VIP_IP,
    VIP_PORT,
    GatewayFabricModel,
    service_gateway,
    service_prefix,
)

DEFAULT_ECN_THRESHOLD = 32
DEFAULT_INT_MTU = 1500


def _member(
    model: GatewayFabricModel, switch: str, peer: str, member_id: int
) -> dict[str, Any]:
    port = model.port_to_peer(switch, peer)
    reverse = model.port_to_peer(peer, switch)
    assert port is not None
    dst_mac = (
        reverse.mac
        if reverse is not None
        else next(item.mac for item in model.endpoints if item.name == peer)
    )
    return {
        "member_id": member_id,
        "src_mac": port.mac,
        "dst_mac": dst_mac,
        "port": port.bmv2_port,
        "peer": peer,
        "role": port.role,
    }


def build_gateway_intent(model: GatewayFabricModel) -> dict[str, Any]:
    """Build roles, LPM routes, ECMP selectors, telemetry, and queue config."""
    switches: dict[str, Any] = {}
    for switch in model.fabric_switches():
        info = model.switch_info[switch]
        members: list[dict[str, Any]] = []
        groups: list[dict[str, Any]] = []
        routes: list[dict[str, Any]] = []
        next_member = 1

        def group(prefix: str, peers: list[str], kind: str, group_id: int) -> None:
            nonlocal next_member
            ids: list[int] = []
            for peer in peers:
                members.append(_member(model, switch, peer, next_member))
                ids.append(next_member)
                next_member += 1
            groups.append({"group_id": group_id, "member_ids": ids, "kind": kind})
            routes.append({"prefix": prefix, "group_id": group_id, "kind": kind})

        if info.role == "gateway":
            client = model.client_on_gateway(switch)
            group(f"{client.ip}/32", [client.name], "local_client", 1)
            for leaf_id in range(1, model.leaf_count + 1):
                group(
                    service_prefix(leaf_id), model.spines, "service_ecmp", 100 + leaf_id
                )
        elif info.role == "spine":
            for gateway_id, gateway in enumerate(model.gateways, start=1):
                client = model.client_on_gateway(gateway)
                group(f"{client.ip}/32", [gateway], "to_gateway", gateway_id)
            for leaf_id, leaf in enumerate(model.leaves, start=1):
                group(service_prefix(leaf_id), [leaf], "to_leaf", 100 + leaf_id)
        else:
            for service_id, service in enumerate(
                model.services_on_leaf(switch), start=1
            ):
                group(f"{service.ip}/32", [service.name], "local_service", service_id)
            for gateway_id, gateway in enumerate(model.gateways, start=1):
                client = model.client_on_gateway(gateway)
                group(f"{client.ip}/32", model.spines, "client_ecmp", 100 + gateway_id)

        ports = [port.bmv2_port for port in model.ports[switch]]
        switches[switch] = {
            "name": switch,
            "role": info.role,
            "role_id": {"gateway": 1, "spine": 2, "leaf": 3}[info.role],
            "device_id": info.device_id,
            "address": f"{info.oob_ip}:{P4RUNTIME_PORT}",
            "telemetry_ip": info.telemetry_ip,
            "members": members,
            "groups": groups,
            "ipv4_lpm": routes,
            "ecn": {str(port): DEFAULT_ECN_THRESHOLD for port in ports},
            "int": {
                "enabled": info.role in {"gateway", "leaf"},
                "source": info.role == "gateway",
                "sink": info.role == "leaf",
                "mtu": DEFAULT_INT_MTU,
            },
        }
        if info.role == "gateway":
            switches[switch]["l4_load_balancer"] = {
                "vip": {"ip": VIP_IP, "port": VIP_PORT},
                "backends": [
                    {"name": backend.name, "dip": backend.ip}
                    for backend in model.backend_pool
                ],
                "hash": {"buckets": 64},
                "pool_version": 1,
            }

    return {
        "topo_size": model.topo_size,
        "pipeline": {"name": PIPELINE_NAME, "ecmp_hash": "5-tuple"},
        "l4_load_balancer": {
            "vip": {"ip": VIP_IP, "port": VIP_PORT, "protocol": "tcp"},
            "backends": [
                {"name": backend.name, "dip": backend.ip}
                for backend in model.backend_pool
            ],
            "hash": {"algorithm": "blake2s", "buckets": 64},
            "conn_table": {
                "capacity": CONN_TABLE_CAPACITY,
                "learning_delay_ms": CONNECTION_LEARNING_DELAY_MS,
            },
            "pool_version": 1,
        },
        "gateways": model.gateways,
        "spines": model.spines,
        "leaves": model.leaves,
        "collector": {"name": "collector", "telemetry_ip": "172.29.0.250"},
        "switches": switches,
        "endpoints": [
            {
                "name": item.name,
                "ip": item.ip,
                "role": item.role,
                "switch": item.attached_switch,
                "gateway_ip": (
                    service_gateway(int(item.attached_switch.rsplit("_", 1)[1]))
                    if item.role == "http_service"
                    else "192.0.2.1"
                ),
                "gateway_mac": VIRTUAL_ROUTER_MAC,
            }
            for item in model.endpoints
        ],
        "web_urls": model.web_urls,
    }
