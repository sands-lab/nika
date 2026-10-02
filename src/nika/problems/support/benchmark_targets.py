"""Shared helpers for failure-owned benchmark inject targets.

Failures declare benchmark targets through ``ProblemBase`` hooks
(``benchmark_inject_params`` / ``benchmark_inject_options`` /
``validate_benchmark_inject``); the benchmark workflow builds the contexts
below and dispatches. Hooks draw only from ``ctx.rng`` so generated cases stay
reproducible. ``nika.problems.registry`` imports this module at load time, so
registry, RCA and ISP modules are imported lazily inside functions.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nika.net_env.base import NetworkEnvBase
    from nika.problems.base import ProblemBase


@dataclass(frozen=True)
class InjectTargetContext:
    """Inputs for resolving one benchmark row's inject params.

    ``rng`` is already advanced by the shared pre-draws (``host0`` .. ``lb0``);
    hooks must draw only from it and in a fixed order.
    """

    problem: str
    scenario: str
    topo_size: str
    seed: int
    isp_options: Mapping[str, Any] | None
    net_env: NetworkEnvBase
    rng: random.Random
    backend: str
    roles: dict[str, list[str]]
    hosts: list[str]
    routers: list[str]
    switches: list[str]
    servers: dict[str, list[str]]
    bmv2: list[str]
    controllers: list[str]
    host_pool: list[str]
    router_pool: list[str]
    host0: str
    router0: str
    dns0: str
    dhcp0: str
    web0: str
    lb0: str


@dataclass(frozen=True)
class InjectValidationContext:
    """Inputs for validating one benchmark row against its topology."""

    scenario: str
    canonical: str
    topo_size: str
    isp_options: Mapping[str, Any] | None
    net_env: NetworkEnvBase
    devices: set[str]
    ifaces_by_device: dict[str, list[str]]


@dataclass(frozen=True)
class MultiInjectContext:
    """Inputs for coordinating the per-failure params of one multi-fault case."""

    scenario: str
    topo_size: str
    net_env: NetworkEnvBase
    rng: random.Random
    backend: str


# ----------------------------------------------------------------------
# Node selection
# ----------------------------------------------------------------------


def choice(rng: random.Random, pool: list[str] | None, fallback: str) -> str:
    items = pool or []
    if not items:
        return fallback
    return rng.choice(items)


def choice_distinct(
    rng: random.Random, pool: list[str] | None, fallback: str, *, n: int = 2
) -> list[str]:
    items = list(pool or [])
    if len(items) >= n:
        return rng.sample(items, n)
    if not items:
        return [fallback] * n
    if len(items) == 1:
        return [items[0], items[0]]
    return items[:n]


def first(items: list[str] | None) -> str | None:
    return items[0] if items else None


def prefer_named(items: list[str], preferred: str, fallback: str) -> str:
    if preferred in items:
        return preferred
    return first(items) or fallback


def prefer_prefixed_node(
    nodes: list[str],
    *,
    prefix: str,
    preferred: str,
    fallback: str,
) -> str:
    filtered = [n for n in nodes if str(n).startswith(prefix)]
    return prefer_named(filtered, preferred, fallback)


def bmv2_leaves(bmv2: list[str]) -> list[str]:
    return [n for n in bmv2 if str(n).startswith("leaf_")]


def arp_l2_endpoint_host(roles: dict[str, list[str]], *, fallback: str) -> str:
    """Pin ARP L2 faults onto the scenario's probe-aligned ``l2_endpoints``."""
    if "l2_endpoints" not in roles:
        return fallback
    endpoints = roles["l2_endpoints"]
    if not endpoints:
        raise ValueError("scenario declares no l2_endpoints hosts for ARP L2 faults")
    return endpoints[0]


def role_subset(
    candidates: list[str], roles: dict[str, list[str]], role: str
) -> list[str]:
    """Candidates holding ``role``; every candidate when none does.

    Used for ``bgp_originators`` (routers with BGP ``network`` statements) and
    ``access_routers`` (routers with end hosts, as ``resolve_victim_host()``
    needs): when the topology does not distinguish the role, keep the pool.
    """
    members = set(roles.get(role) or ())
    return [node for node in candidates if node in members] or list(candidates)


def all_device_names(net_env) -> set[str]:
    names: set[str] = (
        set(net_env.lab.machines.keys()) if net_env.lab is not None else set()
    )
    names.update(net_env.hosts or [])
    names.update(net_env.routers or [])
    names.update(net_env.bmv2_switches or [])
    names.update(net_env.ovs_switches or [])
    names.update(net_env.sdn_controllers or [])
    for bucket in (net_env.servers or {}).values():
        names.update(bucket)
    names.update(getattr(net_env, "kubernetes_nodes", []) or [])
    if net_env.lab is None:
        spec = net_env.get_lab_spec()
        if spec is not None:
            names.update(node.name for node in spec.nodes)
    return names


# ----------------------------------------------------------------------
# Interfaces
# ----------------------------------------------------------------------


def parse_endpoint(endpoint: str) -> tuple[str, str]:
    device, _, intf = endpoint.partition(":")
    return device, intf or ""


def device_interfaces(net_env) -> dict[str, list[str]]:
    mapping: dict[str, set[str]] = defaultdict(set)

    topo = net_env.get_topology()
    if topo:
        for link in topo:
            for endpoint in link:
                device, intf = parse_endpoint(endpoint)
                if device and intf:
                    mapping[device].add(intf)
    else:
        spec = net_env.get_lab_spec()
        if spec is not None:
            for link in spec.links:
                for endpoint in link.endpoints:
                    device, intf = parse_endpoint(endpoint)
                    if device and intf:
                        mapping[device].add(intf)

    return {device: sorted(intfs) for device, intfs in mapping.items()}


def default_interface(backend: str) -> str:
    return "e1-1" if backend == "containerlab" else "eth0"


def choice_interface(
    rng: random.Random,
    net_env,
    device: str,
    backend: str,
) -> str:
    ifaces = device_interfaces(net_env).get(device) or []
    if ifaces:
        return rng.choice(ifaces)
    return default_interface(backend)


def first_iface(ifaces: list[str], fallback: str = "eth0") -> str:
    if not ifaces:
        return fallback
    return sorted(ifaces, key=lambda name: (len(name), name))[0]


# ----------------------------------------------------------------------
# Shared probe-path targets
# ----------------------------------------------------------------------


def resolve_link_flap_params(
    scenario: str,
    net_env,
    rng: random.Random,
    backend: str,
    *,
    host_pool: list[str],
    host0: str,
    router0: str,
) -> dict[str, str]:
    """Pin link_flap inject target and probe path so baseline is healthy and on-path."""
    params: dict[str, str] = {"down_time": "1", "up_time": "1"}

    if scenario in {"sdn_l3_clos", "p4_dc_fabric"}:
        model = getattr(net_env, "model", None)
        if model is not None and getattr(model, "client_endpoints", None):
            observer = model.client_endpoints()[0]
            victim = next(
                web for web in model.web_endpoints() if web.leaf_id != observer.leaf_id
            )
            params.update(
                host_name=observer.name,
                intf_name="eth0",
                probe_dst_ip=victim.ip,
                observer_device=observer.name,
            )
            return params

    if scenario == "p4_dc_gateway":
        model = getattr(net_env, "model", None)
        if model is not None:
            observer = model.clients[0]
            victim = model.backend_pool[0]
            params.update(
                host_name=observer.name,
                intf_name="eth0",
                probe_dst_ip=victim.ip,
                observer_device=observer.name,
            )
            return params

    if scenario == "min3clos":
        params.update(
            host_name="leaf1",
            intf_name="e1-1",
            probe_dst_ip="10.0.0.27",
            observer_device="client1",
        )
        return params

    if scenario == "enterprise_branch":
        corp_src = (
            "br1_corp_pc" if "br1_corp_pc" in host_pool else (first(host_pool) or host0)
        )
        params.update(
            host_name=corp_src,
            intf_name="eth0",
            probe_dst_ip="10.0.20.2",
            observer_device=corp_src,
        )
        return params

    if scenario == "dc_clos":
        params.update(
            host_name="client_0",
            intf_name="eth0",
            probe_dst_ip="10.0.1.2",
            observer_device="client_0",
        )
        return params

    if scenario == "campus_lan":
        params.update(
            host_name="pc_1_1_1_1",
            intf_name="eth0",
            probe_dst_ip="10.200.0.3",
            observer_device="pc_1_1_1_1",
        )
        return params

    params.update(
        host_name=host0,
        intf_name=choice_interface(rng, net_env, host0, backend),
    )
    return params


def resolve_link_corruption_params(
    scenario: str,
    net_env,
    rng: random.Random,
    backend: str,
    *,
    host_pool: list[str],
    host0: str,
    router0: str,
) -> dict[str, str]:
    """Pin link_packet_corruption on the probe path with a low corrupt rate."""
    if scenario in {"sdn_l3_clos", "p4_dc_fabric"}:
        model = getattr(net_env, "model", None)
        if model is not None and getattr(model, "leaf_spine_links", None):
            observer = model.client_endpoints()[0]
            victim = next(
                web for web in model.web_endpoints() if web.leaf_id != observer.leaf_id
            )
            leaf = f"leaf_{observer.leaf_id}"
            spine = next(sp for lf, sp in model.leaf_spine_links if lf == leaf)
            port = model.port_to_peer(leaf, spine)
            return {
                "host_name": leaf,
                "intf_name": port.name if port is not None else "eth2",
                "probe_dst_ip": victim.ip,
                "observer_device": observer.name,
                "corruption_percentage": "12",
            }

    params = resolve_link_flap_params(
        scenario,
        net_env,
        rng,
        backend,
        host_pool=host_pool,
        host0=host0,
        router0=router0,
    )
    params.pop("down_time", None)
    params.pop("up_time", None)
    params["corruption_percentage"] = (
        "10" if scenario in {"enterprise_branch", "p4_dc_gateway"} else "8"
    )
    return params


def resolve_path_mtu_target(
    scenario: str,
    net_env,
    rng: random.Random,
    routers: list[str],
    backend: str,
) -> dict[str, str]:
    """Pick an intermediate L3 egress for real path-MTU reduction."""
    from nika.net_env.isp.identity import is_isp_scenario

    ifaces_by_device = device_interfaces(net_env)
    params: dict[str, str] = {"mtu": "500"}

    if scenario == "dc_clos":
        # Lower MTU on the leaf host-facing egress that serves the default
        # webserver probe target (unique hop; avoids SS ECMP bypass).
        from nika.problems.rca.inventory import (
            iter_link_termination_points,
            parse_endpoint as parse_rca_endpoint,
        )

        web_hosts = list((getattr(net_env, "servers", None) or {}).get("web") or [])
        host = None
        intf = None
        for web in web_hosts:
            needle_prefix = f"{web}:"
            for _key, tps in iter_link_termination_points(net_env):
                endpoints = [str(ep) for ep in tps]
                web_ep = next(
                    (ep for ep in endpoints if ep.startswith(needle_prefix)), None
                )
                if web_ep is None or len(endpoints) != 2:
                    continue
                other = endpoints[0] if endpoints[1] == web_ep else endpoints[1]
                peer_host, peer_intf = parse_rca_endpoint(other)
                if peer_host.startswith("leaf_router_"):
                    host = peer_host
                    intf = peer_intf
                    break
            if host is not None:
                break
        if host is None:
            candidates = [n for n in routers if str(n).startswith("leaf_router_")]
            host = (
                "leaf_router_0_1"
                if "leaf_router_0_1" in candidates
                else choice(rng, candidates, "leaf_router_0_0")
            )
            host_ifaces = ifaces_by_device.get(host) or ["eth0"]
            ordered = sorted(host_ifaces, key=lambda name: (len(name), name))
            intf = ordered[-1] if ordered else "eth0"
        params.update(host_name=host, intf_name=intf or "eth0")
        return params

    if scenario == "campus_lan":
        candidates = [
            n for n in routers if "router_core" in n or "router_dist" in n
        ] or list(routers)
        host = choice(rng, candidates, candidates[0] if candidates else "router_core_1")
        params.update(
            host_name=host,
            intf_name=first_iface(ifaces_by_device.get(host) or [], "eth0"),
        )
        return params

    if scenario == "enterprise_branch":
        host = (
            "br1_edge"
            if "br1_edge" in routers
            else choice(rng, list(routers), "br1_edge")
        )
        host_ifaces = ifaces_by_device.get(host) or []
        if "eth2" in host_ifaces:
            intf = "eth2"
        elif host_ifaces:
            intf = host_ifaces[-1]
        else:
            intf = "eth2"
        params.update(host_name=host, intf_name=intf)
        return params

    if scenario == "k8s_lab":
        # Unique hop on client → controller (201.1.1.2): leaf_1_1 host-facing
        # eth2. Spine-facing eth0/eth1 are BGP unnumbered; shrinking those
        # MTUs drops the fabric session and even small pings fail.
        if "leaf_1_1" in routers:
            host = "leaf_1_1"
        else:
            host = choice(rng, list(routers), "leaf_1_1")
        host_ifaces = ifaces_by_device.get(host) or []
        if "eth2" in host_ifaces:
            intf = "eth2"
        else:
            intf = first_iface(host_ifaces, "eth2")
        params.update(host_name=host, intf_name=intf)
        return params

    if is_isp_scenario(scenario):
        from nika.net_env.isp.inject_targets import isp_inject_params

        inventory = getattr(net_env, "inventory", None)
        if not isinstance(inventory, dict):
            inventory = {}
        link_params = isp_inject_params(
            "link_capacity_bottleneck", inventory, inventory.get("bgp")
        )
        params.update(
            host_name=str(link_params["host_name"]),
            intf_name=str(link_params["intf_name"]),
        )
        return params

    host = choice(rng, list(routers), "router1")
    params.update(
        host_name=host,
        intf_name=choice_interface(rng, net_env, host, backend),
    )
    return params


def isp_protocol_for(
    problem_cls: type[ProblemBase] | None, tags: set[str]
) -> dict[str, Any]:
    """Pick ISP protocol options from failure needs (topology comes from scenario).

    A failure's declared ``isp_protocol`` wins; otherwise the stack follows
    ``tags`` plus the failure's own TAGS.
    """
    from nika.net_env.isp.bgp.config import DEFAULT_BGP_MODE
    from nika.net_env.isp.igp.config import DEFAULT_IGP

    if problem_cls is not None:
        if problem_cls.isp_protocol is not None:
            return dict(problem_cls.isp_protocol)
        tags = set(tags) | set(problem_cls.TAGS)
    if "ospf" in tags:
        return {"igp": "ospf", "bgp_mode": "none", "rpki": False}
    if "bgp" in tags:
        return {"igp": DEFAULT_IGP, "bgp_mode": "ibgp_rr", "rpki": False}
    return {"igp": DEFAULT_IGP, "bgp_mode": DEFAULT_BGP_MODE, "rpki": False}


# ----------------------------------------------------------------------
# Cross-domain inject targets
# ----------------------------------------------------------------------


def endpoint_target(
    ctx: InjectTargetContext,
    *,
    link: bool = False,
    detach: bool = False,
    l2_endpoint: bool = False,
    with_intf: bool = False,
) -> dict[str, str]:
    """Host or host-attachment target shared by link and host-config faults.

    ``link`` also picks the attachment interface, ``detach`` pins access links
    that stay off the controller fabric, ``l2_endpoint`` anchors on the
    ``l2_endpoints`` role, and ``with_intf`` picks an interface for host faults.
    """
    rng, net_env, backend, scenario = ctx.rng, ctx.net_env, ctx.backend, ctx.scenario
    host_pool, host0 = ctx.host_pool, ctx.host0
    params: dict[str, str] = {}
    if detach and scenario in {"sdn_l3_clos", "p4_dc_fabric"}:
        # Detach a client access link; removing fabric switch ports breaks controllers.
        params["host_name"] = prefer_named(host_pool, "client_1_1", host0)
        params["intf_name"] = "eth0"
    elif detach and scenario == "min3clos":
        client = prefer_named(host_pool, "client2", host0)
        params["host_name"] = client
        params["intf_name"] = choice_interface(rng, net_env, client, backend)
    elif detach and scenario == "campus_lan":
        # Detach web0.local (10.200.0.3); LB backends are off the default probe path.
        web_pool = list(ctx.servers.get("web") or []) or list(
            ctx.roles.get("web") or []
        )
        params["host_name"] = prefer_named(web_pool, "web_server_0", ctx.web0 or host0)
        params["intf_name"] = "eth0"
    elif scenario in {"sdn_l3_clos", "p4_dc_fabric"} and link:
        # Prefer a leaf–spine fabric link so Clos path/ECMP failures are exercised.
        model = getattr(net_env, "model", None)
        if model is not None and getattr(model, "leaf_spine_links", None):
            leaf, spine = rng.choice(model.leaf_spine_links)
            port = model.port_to_peer(leaf, spine)
            params["host_name"] = leaf
            params["intf_name"] = port.name if port is not None else "eth2"
        else:
            switches = net_env.ovs_switches or net_env.bmv2_switches or []
            leaf = choice(rng, bmv2_leaves(switches) or switches, "leaf_1")
            params["host_name"] = leaf
            params["intf_name"] = choice_interface(rng, net_env, leaf, backend)
    elif scenario == "min3clos" and link:
        params["host_name"] = ctx.router0
        params["intf_name"] = choice_interface(rng, net_env, ctx.router0, backend)
    elif scenario == "enterprise_branch":
        if l2_endpoint:
            corp_src = arp_l2_endpoint_host(ctx.roles, fallback=host0)
        else:
            corp_src = (
                "br1_corp_pc"
                if "br1_corp_pc" in host_pool
                else first(host_pool) or host0
            )
        params["host_name"] = corp_src
        if link:
            params["intf_name"] = "eth0"
        elif with_intf:
            params["intf_name"] = choice_interface(rng, net_env, corp_src, backend)
    else:
        if l2_endpoint:
            params["host_name"] = arp_l2_endpoint_host(ctx.roles, fallback=host0)
        else:
            params["host_name"] = host0
        if link or with_intf:
            params["intf_name"] = choice_interface(rng, net_env, host0, backend)
    return params


def isp_link_target(ctx: InjectTargetContext) -> dict[str, str]:
    """ISP backbone link for ``ctx.problem`` plus its symptom observers."""
    from nika.net_env.isp.inject_targets import (
        isp_inject_params,
        isp_link_symptom_targets,
    )

    inventory = getattr(ctx.net_env, "inventory", None)
    if not isinstance(inventory, dict):
        inventory = {}
    params: dict[str, str] = {}
    params.update(isp_inject_params(ctx.problem, inventory, inventory.get("bgp")))
    device = params.get("host_name")
    iface = params.get("intf_name")
    if device and iface:
        params.update(isp_link_symptom_targets(inventory, device, iface))
    return params


def p4_gateway_port_target(
    ctx: InjectTargetContext, *, gateways_only: bool
) -> dict[str, str]:
    """Fabric-facing egress port on a P4 gateway (or spine) switch.

    Callers draw ``rng.choice(model.services)`` first, as the shared P4
    gateway resolution order requires.
    """
    rng = ctx.rng
    model = ctx.net_env.model
    if gateways_only:
        target = rng.choice(model.gateways)
        peer = rng.choice(model.spines)
    else:
        target = rng.choice(model.gateways + model.spines)
        candidates = [
            port for port in model.ports[target] if port.role in {"spine", "leaf"}
        ]
        peer = rng.choice(candidates).peer
    port = model.port_to_peer(target, peer)
    assert port is not None
    return {
        "host_name": target,
        "intf_name": port.name,
        "bmv2_port": str(port.bmv2_port),
    }


def access_router_victim(
    ctx: InjectTargetContext, *, prefer_site_edge: bool
) -> dict[str, str]:
    """Router whose attached end hosts lose reachability (ISP-aware).

    ``prefer_site_edge`` pins ``hq_edge`` / ``br1_edge`` on enterprise_branch
    without drawing.
    """
    from nika.net_env.isp.identity import is_isp_scenario

    params: dict[str, str] = {}
    if is_isp_scenario(ctx.scenario):
        from nika.net_env.isp.inject_targets import isp_inject_params

        inventory = getattr(ctx.net_env, "inventory", None)
        if not isinstance(inventory, dict):
            inventory = {}
        bgp_inv = inventory.get("bgp")
        params.update(
            isp_inject_params(
                ctx.problem,
                inventory,
                bgp_inv if isinstance(bgp_inv, dict) else None,
            )
        )
    elif ctx.scenario == "enterprise_branch" and prefer_site_edge:
        victim_pool = role_subset(ctx.router_pool, ctx.roles, "access_routers")
        params["host_name"] = (
            "hq_edge"
            if "hq_edge" in victim_pool
            else (
                "br1_edge"
                if "br1_edge" in victim_pool
                else first(victim_pool) or ctx.router0
            )
        )
    else:
        victim_pool = role_subset(ctx.router_pool, ctx.roles, "access_routers")
        params["host_name"] = choice(
            ctx.rng, victim_pool, first(victim_pool) or ctx.router0
        )
    return params


def dhcp_server_client(ctx: InjectTargetContext) -> dict[str, str]:
    """DHCP server (``host_name``) and one of its clients (``host_name_2``)."""
    host_pool = ctx.host_pool
    client = choice(
        ctx.rng,
        [h for h in host_pool if h != ctx.dhcp0] or host_pool,
        ctx.host0,
    )
    if ctx.scenario == "campus_lan" and "pc_1_1_1_1" in host_pool:
        client = "pc_1_1_1_1"
    return {"host_name": ctx.dhcp0, "host_name_2": client}


def k8s_control_node(ctx: InjectTargetContext) -> str:
    """Kubernetes control-plane node (first controller, else first node)."""
    k8s_nodes = ctx.roles.get("k8s_nodes") or []
    return first(ctx.roles.get("k8s_controllers")) or first(k8s_nodes) or ctx.host0


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------


def require_role_member(
    ctx: InjectValidationContext,
    problem: str,
    host_name: str | None,
    role: str,
    *,
    reason: str,
) -> None:
    """Reject a router outside ``role`` when the topology distinguishes it."""
    if not host_name:
        return
    routers = ctx.net_env.routers or []
    eligible = role_subset(routers, ctx.net_env.target_roles(), role)
    if eligible != list(routers) and host_name not in eligible:
        raise ValueError(
            f"{problem} host_name={host_name!r} {reason} on "
            f"{ctx.scenario} (topo_size={ctx.topo_size!r}); "
            f"use a leaf router: {eligible}"
        )


def require_distinct_hosts(
    ctx: InjectValidationContext,
    problem: str,
    inject: Mapping[str, str],
    *,
    switches: bool = False,
) -> None:
    """Reject ``host_name == host_name_2`` when two candidates exist.

    Candidates are hosts plus servers, or OVS switches with ``switches``.
    """
    host_a = inject.get("host_name")
    host_b = inject.get("host_name_2")
    if not (host_a and host_b and host_a == host_b):
        return
    net_env = ctx.net_env
    if switches:
        candidates = list(net_env.ovs_switches or [])
        noun = "OVS switches"
    else:
        server_hosts: list[str] = []
        for bucket in (net_env.servers or {}).values():
            server_hosts.extend(bucket or [])
        candidates = list(dict.fromkeys(list(net_env.hosts or []) + server_hosts))
        noun = "hosts"
    if len(candidates) >= 2:
        raise ValueError(
            f"Inject devices host_name and host_name_2 must differ for {problem} "
            f"on {ctx.scenario} when multiple {noun} exist"
        )


# ----------------------------------------------------------------------
# Enumeration strategies
# ----------------------------------------------------------------------


def unique(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    by_key = {tuple(sorted(row.items())): row for row in rows}
    return [by_key[key] for key in sorted(by_key)]


def replace(base: dict[str, str], **values: str) -> dict[str, str]:
    return {**base, **{key: str(value) for key, value in values.items()}}


def role_nodes(net_env: Any, base_node: str) -> list[str]:
    servers = getattr(net_env, "servers", None) or {}
    for nodes in servers.values():
        if base_node in (nodes or []):
            return sorted(nodes)
    pools = (
        getattr(net_env, "kubernetes_nodes", None) or [],
        getattr(net_env, "sdn_controllers", None) or [],
        getattr(net_env, "bmv2_switches", None) or [],
        getattr(net_env, "ovs_switches", None) or [],
        getattr(net_env, "routers", None) or [],
        getattr(net_env, "hosts", None) or [],
    )
    for nodes in pools:
        if base_node not in nodes:
            continue
        if base_node.startswith(("leaf_", "gateway_", "spine_")):
            prefix = base_node.partition("_")[0] + "_"
            return sorted(node for node in nodes if node.startswith(prefix))
        return sorted(nodes)
    return [base_node]


def resource(ctx: InjectTargetContext, base: dict[str, str]):
    """Single root-cause resource of the canonical row, else ``None``."""
    from nika.problems.rca.materialize import ground_truth_for_case

    truth = ground_truth_for_case(
        problem=ctx.problem,
        params=base,
        scenario=ctx.scenario,
        topo_size=ctx.topo_size,
        net_env=ctx.net_env,
    )
    if len(truth.root_causes) != 1:
        return None
    return truth.root_causes[0].resource


_NODE_FIELDS = (
    "host_name",
    "host_name_2",
    "forwarding_device",
    "attacker_device",
    "node_name",
)


def node_target_options(
    problem_cls: type[ProblemBase], ctx: InjectTargetContext, base: dict[str, str]
) -> list[dict[str, str]]:
    """Swap the root-cause node for every node sharing its role.

    ``problem_cls.benchmark_node_targets`` narrows the candidates and
    ``benchmark_align_option`` adjusts each row.
    """
    found = resource(ctx, base)
    if found is None or str(found.kind) != "node":
        return [base]
    net_env = ctx.net_env
    node = str(found.node or "")
    field = next((key for key in _NODE_FIELDS if base.get(key) == node), None)
    if field is None:
        return [base]
    targets = problem_cls.benchmark_node_targets(ctx, role_nodes(net_env, node))
    rows: list[dict[str, str]] = []
    for target in targets:
        row = replace(base, **{field: target})
        other = "host_name_2" if field == "host_name" else "host_name"
        if field in {"host_name", "host_name_2"} and row.get(other) == target:
            donor = next(
                (item for item in role_nodes(net_env, node) if item != target), None
            )
            if donor is None:
                continue
            row[other] = donor
        rows.append(problem_cls.benchmark_align_option(ctx, row, field))
    return rows


def link_target_options(
    ctx: InjectTargetContext, base: dict[str, str], *, point_to_point: bool
) -> list[dict[str, str]]:
    """One row per topology link, keeping the canonical auxiliary knobs.

    ``point_to_point`` keeps only 2-endpoint links (Kathara VDE proxy faults).
    On ISP scenarios each row gets per-link symptom observers.
    """
    from nika.net_env.isp.identity import is_isp_scenario
    from nika.problems.rca.inventory import (
        iter_link_termination_points,
        parse_endpoint as parse_rca_endpoint,
    )

    net_env = ctx.net_env
    isp = is_isp_scenario(ctx.scenario)
    drop = {"host_name", "intf_name"}
    if isp:
        # Per-link observers; do not freeze canonical first-link probes.
        drop |= {"symptom_host", "probe_dst_ip", "peer_host"}
    aux = {key: value for key, value in base.items() if key not in drop}
    link_base = {"host_name": base["host_name"], "intf_name": base["intf_name"]}
    interfaces = device_interfaces(net_env)
    options: list[dict[str, str]] = []
    for _key, raw_endpoints in iter_link_termination_points(net_env):
        parsed = sorted(parse_rca_endpoint(str(item)) for item in raw_endpoints)
        if len(parsed) < 2:
            continue
        if point_to_point and len(parsed) != 2:
            continue
        eligible = [item for item in parsed if item[1] in interfaces.get(item[0], ())]
        node, intf = (eligible or parsed)[0]
        options.append(replace(link_base, host_name=node, intf_name=intf))
    rows = [{**aux, **row} for row in options]
    if isp:
        from nika.net_env.isp.inject_targets import isp_link_symptom_targets

        inventory = getattr(net_env, "inventory", None) or {}
        enriched: list[dict[str, str]] = []
        for row in rows:
            try:
                targets = isp_link_symptom_targets(
                    inventory, row["host_name"], row["intf_name"]
                )
            except ValueError:
                # Stub/edge attachments are outside inventory backbone links.
                continue
            enriched.append({**row, **targets})
        rows = enriched
    return unique(rows)


def p4_port_options(
    ctx: InjectTargetContext, base: dict[str, str], *, gateways_only: bool
) -> list[dict[str, str]]:
    """Every fabric-facing port on P4 gateways (plus spines unless ``gateways_only``)."""
    model = getattr(ctx.net_env, "model", None)
    nodes = list(getattr(model, "gateways", None) or [])
    if not gateways_only:
        nodes += list(getattr(model, "spines", None) or [])
    rows: list[dict[str, str]] = []
    for node in sorted(nodes):
        for port in sorted(
            getattr(model, "ports", {}).get(node, []), key=lambda item: item.name
        ):
            if port.role in {"spine", "leaf"}:
                rows.append(
                    replace(
                        base,
                        host_name=node,
                        intf_name=port.name,
                        bmv2_port=str(port.bmv2_port),
                    )
                )
    return rows
