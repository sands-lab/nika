"""RFC 8345-style termination-point inventory for root-cause mapping.

Do not use ``NetworkEnvBase.get_topology()`` for interface inventory: Kathara
implementation keeps only the first two members of a collision domain.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from nika.net_env.isp.profiles import DEFAULT_BACKEND_FOR_ISP
from nika.net_env.net_env_pool import get_net_env_instance, resolve_scenario_backend
from nika.problems.root_cause import (
    FaultResource,
    interface_resource,
    k8s_resource,
    node_resource,
)

_INTF_TAIL_RE = re.compile(r"(\d+)(?:-(\d+))?$")


def parse_endpoint(endpoint: str) -> tuple[str, str]:
    device, _, intf = endpoint.partition(":")
    return device, intf or ""


def iter_link_termination_points(net_env: Any) -> list[tuple[str, tuple[str, ...]]]:
    """Return ``[(link_key, (node:intf, ...)), ...]`` with every TP on the link."""
    rows: list[tuple[str, tuple[str, ...]]] = []
    seen: set[tuple[str, ...]] = set()

    def _add(link_key: str, tps: tuple[str, ...]) -> None:
        key = tuple(sorted(tps))
        if len(tps) < 2 or key in seen:
            return
        seen.add(key)
        rows.append((link_key, tps))

    inventory = getattr(net_env, "inventory", None)
    if isinstance(inventory, dict) and inventory.get("links"):
        for link in inventory["links"]:
            if not isinstance(link, dict):
                continue
            eps: list[str] = []
            for key in ("endpoint_a", "endpoint_b"):
                ep = link.get(key) or {}
                if isinstance(ep, dict):
                    device = str(ep.get("device") or "")
                    iface = str(ep.get("iface") or "")
                    if device and iface:
                        eps.append(f"{device}:{iface}")
            if len(eps) >= 2:
                link_id = str(link.get("link_id") or f"{eps[0]}--{eps[1]}")
                _add(link_id, tuple(eps))

    lab = getattr(net_env, "lab", None)
    if lab is not None and getattr(lab, "machines", None):
        by_link: dict[str, list[str]] = defaultdict(list)
        for machine, stat in lab.machines.items():
            interfaces = getattr(stat, "interfaces", None) or {}
            for intf_num, intf in interfaces.items():
                link_obj = getattr(intf, "link", None)
                link_name = getattr(link_obj, "name", None)
                if not link_name:
                    continue
                by_link[str(link_name)].append(f"{machine}:eth{intf_num}")
        for name, tps in sorted(by_link.items()):
            _add(name, tuple(tps))

    spec = None
    if hasattr(net_env, "get_lab_spec"):
        spec = net_env.get_lab_spec()
    if spec is not None:
        for index, link in enumerate(getattr(spec, "links", None) or []):
            endpoints = tuple(str(item) for item in (link.endpoints or ()))
            if endpoints:
                _add(f"link-{index}", endpoints)

    return rows


def interfaces_for_node(net_env: Any, node: str) -> list[str]:
    names: set[str] = set()
    for _key, tps in iter_link_termination_points(net_env):
        for endpoint in tps:
            device, intf = parse_endpoint(endpoint)
            if device == node and intf:
                names.add(intf)
    return sorted(names, key=_intf_sort_key)


def _intf_sort_key(name: str) -> tuple[int, int, str]:
    match = _INTF_TAIL_RE.search(name)
    if match:
        return (int(match.group(1)), int(match.group(2) or 0), name)
    return (999, 0, name)


def net_env_backend(net_env: Any) -> str:
    return str(getattr(net_env, "backend", None) or "kathara")


def resolve_default_intf(intf_name: str, net_env: Any) -> str:
    if intf_name != "eth0":
        return intf_name
    return "e1-1" if net_env_backend(net_env) == "containerlab" else "eth0"


def select_host_interface(net_env: Any, host_name: str, *, last: bool) -> str:
    """Match inject defaults: Containerlab ``e1-1``; Kathara first/last topo iface."""
    if net_env_backend(net_env) == "containerlab":
        return "e1-1"
    ifaces = interfaces_for_node(net_env, host_name)
    if not ifaces:
        return "eth0"
    return ifaces[-1] if last else ifaces[0]


def interface_on(net_env: Any, node: str, intf: str):
    return interface_resource(node, resolve_default_intf(intf, net_env))


def catalog_resources(
    net_env: Any,
    *,
    k8s_services: list[dict] | None = None,
    k8s_network_policies: list[dict] | None = None,
) -> list[FaultResource]:
    """Closed catalog: every node and interface, plus optional live k8s objects."""
    nodes: set[str] = set()
    interfaces: list[tuple[str, str]] = []
    seen_iface: set[tuple[str, str]] = set()

    lab = getattr(net_env, "lab", None)
    machines = getattr(lab, "machines", None) if lab is not None else None
    if isinstance(machines, dict):
        nodes.update(str(name) for name in machines)

    for _key, tps in iter_link_termination_points(net_env):
        for endpoint in tps:
            device, intf = parse_endpoint(endpoint)
            if device:
                nodes.add(device)
            if device and intf and (device, intf) not in seen_iface:
                seen_iface.add((device, intf))
                interfaces.append((device, intf))

    items: list[FaultResource] = [node_resource(name) for name in sorted(nodes)]
    items.extend(interface_resource(node, intf) for node, intf in interfaces)

    for svc in k8s_services or []:
        name = str(svc.get("name") or "")
        ns = str(svc.get("namespace") or "")
        if name:
            items.append(k8s_resource("Service", name, namespace=ns or None))
    for policy in k8s_network_policies or []:
        name = str(policy.get("name") or "")
        ns = str(policy.get("namespace") or "")
        if name:
            items.append(k8s_resource("NetworkPolicy", name, namespace=ns or None))
    return items


def load_offline_net_env(
    scenario: str,
    topo_size: str = "",
    *,
    workload: str | None = None,
):
    """Instantiate a scenario without deploying, for GT generation and migration."""
    kwargs: dict = {}
    if topo_size:
        kwargs["topo_size"] = topo_size
    if workload is not None:
        kwargs["workload"] = workload
    kwargs["backend"] = resolve_scenario_backend(
        scenario, default_when_ambiguous=DEFAULT_BACKEND_FOR_ISP
    )
    net_env = get_net_env_instance(scenario, **kwargs)
    if getattr(net_env, "lab", None) is not None:
        net_env.load_machines()
    return net_env
