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
from nika.problems.rca.models import (
    FaultResource,
    UnresolvedRootCauseError,
    interface_resource,
    k8s_resource,
    link_resource,
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


def first_data_interface(net_env: Any, node: str) -> str:
    """Return ``node``'s first data interface in the environment topology.

    Containerlab reserves container ``eth0`` for management, so the data
    ports keep the platform's names (``e1-1`` on SR Linux, ``eth1`` on SONiC
    and Linux nodes). A node missing from the topology gets the platform's
    first port.
    """
    ifaces = interfaces_for_node(net_env, node)
    if ifaces:
        return ifaces[0]
    if net_env_backend(net_env) != "containerlab":
        return "eth0"
    srl = getattr(net_env, "device_profile", None) in (None, "", "nokia_srlinux")
    return "e1-1" if srl else "eth1"


def resolve_default_intf(intf_name: str, net_env: Any, node: str) -> str:
    """Resolve the ``eth0`` placeholder to ``node``'s first data interface.

    Kathara data interfaces start at ``eth0``, so the name stays as given.
    """
    if intf_name != "eth0" or net_env_backend(net_env) != "containerlab":
        return intf_name
    return first_data_interface(net_env, node)


def _sonic_router(net_env: Any, node: str) -> bool:
    identity = (getattr(net_env, "machine_identities", None) or {}).get(node)
    return identity is not None and "sonic" in getattr(identity, "capabilities", ())


def kernel_data_interface(net_env: Any, node: str, intf: str | None = None) -> str:
    """Kernel name of a topology data interface on ``node``.

    Names come from the environment inventory, never from the management
    network: Containerlab ``eth0`` is replaced by the node's first data port.
    SONiC routers map container ``eth{k}`` to ``Ethernet{4(k-1)}``. Kathara
    names pass through.
    """
    name = (
        first_data_interface(net_env, node)
        if not intf
        else resolve_default_intf(intf, net_env, node)
    )
    if not _sonic_router(net_env, node) or name.startswith("Ethernet"):
        return name
    from nika.net_env.utils.sonic.common import sonic_port

    try:
        return sonic_port(name)
    except ValueError:
        return name


def select_host_interface(net_env: Any, host_name: str, *, last: bool) -> str:
    """Match inject defaults: Containerlab first data port; Kathara first/last."""
    if net_env_backend(net_env) == "containerlab":
        return first_data_interface(net_env, host_name)
    ifaces = interfaces_for_node(net_env, host_name)
    if not ifaces:
        return "eth0"
    return ifaces[-1] if last else ifaces[0]


def interface_on(net_env: Any, node: str, intf: str):
    return interface_resource(node, resolve_default_intf(intf, net_env, node))


def canonical_link_name(tps: tuple[str, ...]) -> str:
    """Stable undirected link body: sorted ``node:intf`` TPs joined by ``--``."""
    cleaned: list[str] = []
    for endpoint in tps:
        device, intf = parse_endpoint(str(endpoint))
        if not device or not intf:
            raise ValueError(f"link TP requires node and interface: {endpoint!r}")
        cleaned.append(f"{device}:{intf}")
    if len(cleaned) < 2:
        raise ValueError(f"link requires at least two TPs, got {tps!r}")
    return "--".join(sorted(cleaned))


def link_resource_from_tps(tps: tuple[str, ...]) -> FaultResource:
    return link_resource(canonical_link_name(tps))


def link_containing_endpoint(net_env: Any, node: str, intf: str) -> FaultResource:
    """Resolve the unique catalog link that contains ``node``/``intf``."""
    resolved = resolve_default_intf(intf, net_env, node)
    needle = f"{node}:{resolved}"
    matches: list[FaultResource] = []
    for _key, tps in iter_link_termination_points(net_env):
        endpoints = {str(ep) for ep in tps}
        if needle in endpoints:
            matches.append(link_resource_from_tps(tuple(str(ep) for ep in tps)))
    if not matches:
        raise UnresolvedRootCauseError(
            f"No link contains endpoint {needle!r} in scenario inventory."
        )
    if len(matches) > 1:
        ids = ", ".join(sorted(m.id for m in matches))
        raise UnresolvedRootCauseError(
            f"Endpoint {needle!r} matches multiple links: {ids}."
        )
    return matches[0]


def catalog_resources(
    net_env: Any,
    *,
    k8s_services: list[dict] | None = None,
    k8s_network_policies: list[dict] | None = None,
) -> list[FaultResource]:
    """Closed catalog: every node, interface, and link, plus optional live k8s objects."""
    nodes: set[str] = set()
    interfaces: list[tuple[str, str]] = []
    seen_iface: set[tuple[str, str]] = set()
    links: list[FaultResource] = []
    seen_link: set[str] = set()

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
        link = link_resource_from_tps(tuple(str(ep) for ep in tps))
        if link.id not in seen_link:
            seen_link.add(link.id)
            links.append(link)

    overlay = getattr(net_env, "overlay_interfaces", None)
    for device, intf in overlay() if callable(overlay) else []:
        nodes.add(device)
        if (device, intf) not in seen_iface:
            seen_iface.add((device, intf))
            interfaces.append((device, intf))

    items: list[FaultResource] = [node_resource(name) for name in sorted(nodes)]
    items.extend(interface_resource(node, intf) for node, intf in interfaces)
    items.extend(links)

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
    topo: str | None = None,
    igp: str | None = None,
    bgp_mode: str | None = None,
    rpki: bool | None = None,
    backend: str | None = None,
    device_profile: str | None = None,
):
    """Instantiate a scenario without deploying, for GT generation and migration."""
    from nika.net_env.isp.identity import (
        is_isp_named_special,
        is_isp_scenario,
    )

    kwargs: dict = {}
    # ISP scenarios bake topology into the scenario ID and reject topo_size.
    if topo_size and not is_isp_scenario(scenario):
        kwargs["topo_size"] = topo_size
    if topo is not None and not is_isp_scenario(scenario):
        kwargs["topo"] = topo
    if is_isp_named_special(scenario):
        # Fixed protocol profile; ignore optional overrides.
        pass
    else:
        if igp is not None:
            kwargs["igp"] = igp
        if bgp_mode is not None:
            kwargs["bgp_mode"] = bgp_mode
        if rpki is not None:
            kwargs["rpki"] = bool(rpki)
    kwargs["backend"] = resolve_scenario_backend(
        scenario,
        backend=backend,
        default_when_ambiguous=DEFAULT_BACKEND_FOR_ISP,
    )
    if device_profile is not None:
        kwargs["device_profile"] = device_profile
    net_env = get_net_env_instance(scenario, **kwargs)
    if getattr(net_env, "lab", None) is not None:
        net_env.load_machines()
    return net_env


def load_session_offline_net_env(row: dict):
    """Offline scenario for a SessionStore row, matching its recorded variant.

    Passes the session's backend / device profile / RPKI so resource ids match
    the ground truth (e.g. Containerlab ``e1-1`` vs Kathara ``eth*`` names).
    """
    params = row.get("scenario_params") or {}
    return load_offline_net_env(
        str(row.get("scenario_name") or ""),
        str(row.get("scenario_topo_size") or params.get("topo_size") or ""),
        topo=params.get("topo"),
        igp=params.get("igp"),
        bgp_mode=params.get("bgp_mode"),
        rpki=params.get("rpki"),
        backend=params.get("backend"),
        device_profile=params.get("device_profile"),
    )
