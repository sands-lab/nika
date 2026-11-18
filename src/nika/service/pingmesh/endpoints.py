"""Discover endpoint hosts suitable for PingMesh probing."""

from __future__ import annotations

from typing import Any

from nika.service.containerlab.base_api import ContainerlabBaseAPI
from nika.runtime.spec import MachineInventory

ENDPOINT_NAME_KEYS = ("client", "pc", "host", "server")
EXCLUDED_NAME_KEYS = ("router", "switch", "leaf", "spine", "sw")
CONTAINERLAB_PROBE_IFACES = ("eth1", "eth0")


def is_excluded_node_name(name: str) -> bool:
    lowered = name.lower()
    return any(key in lowered for key in EXCLUDED_NAME_KEYS)


def is_endpoint_node_name(name: str) -> bool:
    lowered = name.lower()
    if is_excluded_node_name(name):
        return False
    return any(key in lowered for key in ENDPOINT_NAME_KEYS)


def discover_endpoints(api: Any) -> list[str]:
    """Return sorted endpoint host names for the current lab backend."""
    if isinstance(api, ContainerlabBaseAPI):
        nodes = api.runtime.list_nodes()
        endpoints = sorted(name for name in nodes if is_endpoint_node_name(name))
        return endpoints

    api.load_machines()
    endpoints: list[str] = list(api.hosts)
    inventory = getattr(api, "machine_inventory", None)
    if isinstance(inventory, MachineInventory):
        endpoints.extend(inventory.reachability_targets())
    for servers in api.servers.values():
        for server in servers:
            if server not in endpoints:
                endpoints.append(server)
    return sorted(set(endpoints))


def resolve_endpoint_ip(api: Any, host_name: str) -> str | None:
    """Return the data-plane IP used for endpoint-to-endpoint probing."""
    if isinstance(api, ContainerlabBaseAPI):
        for iface in CONTAINERLAB_PROBE_IFACES:
            ip = api.get_host_ip(host_name, iface)
            if ip:
                return ip
        return None
    return api.get_host_ip(host_name)
