"""Compile NIKA BGP presets onto an IspPlan."""

from __future__ import annotations

import heapq
from dataclasses import dataclass, replace
from ipaddress import IPv4Network
from typing import Any, Literal

from nika.net_env.isp.bgp.config import (
    DEFAULT_BGP_MODE,
    EBGP_BASE_ASN,
    IBGP_ASN,
    IspBgpMode,
    normalize_bgp_mode,
)
from nika.net_env.isp.bgp.errors import BgpCompileError, BgpConfigError
from nika.net_env.isp.igp.frr import render_frr_conf
from nika.net_env.isp.igp.plan import IspPlan, PlannedNode, build_inventory

BgpSessionType = Literal["ibgp", "ebgp"]
BgpRole = Literal["rr", "client", "member", "border", "originator"]


@dataclass(frozen=True)
class BgpSession:
    local_device: str
    remote_device: str
    local_ip: str
    remote_ip: str
    local_asn: int
    remote_asn: int
    session_type: BgpSessionType
    update_source: str | None  # "lo" for iBGP; None for eBGP link IPs
    route_reflector_client: bool = False


@dataclass(frozen=True)
class BgpOriginatedPrefix:
    device: str
    prefix: str  # e.g. 203.0.113.0/24
    ping_address: str  # host address installed on lo for reachability checks


@dataclass(frozen=True)
class BgpNodePlan:
    device_name: str
    asn: int
    roles: tuple[str, ...]
    router_id: str
    sessions: tuple[BgpSession, ...]
    originated: tuple[BgpOriginatedPrefix, ...]
    cluster_id: str | None = None
    rov_reject_invalid: bool = False
    rpki_cache: tuple[str, int] | None = None  # (ip, port)
    export_deny_prefixes: tuple[str, ...] = ()
    rtbh_import_policy: bool = False
    ebgp_outbound_route_maps: tuple[tuple[str, str], ...] = ()  # (neighbor_ip, map)


@dataclass(frozen=True)
class BgpPlan:
    mode: IspBgpMode
    topology_name: str
    nodes: tuple[BgpNodePlan, ...]
    sessions: tuple[BgpSession, ...]
    originated: tuple[BgpOriginatedPrefix, ...]
    # Verify helpers
    expect_reachable: tuple[tuple[str, str], ...]  # (observer_device, prefix)
    deny_prefixes: tuple[str, ...]
    inventory: dict[str, Any]


def compile_bgp_plan(
    isp_plan: IspPlan,
    mode: IspBgpMode | str | None = DEFAULT_BGP_MODE,
    *,
    rpki: bool = False,
    rtbh: bool = False,
) -> BgpPlan | None:
    """Build a BGP plan from a NIKA preset, or None when mode is none.

    ``rpki=True`` overlays the offline RPKI/ROV profile onto eBGP.
    ``rtbh=True`` overlays the RTBH community blackhole profile onto eBGP.
    """
    resolved = normalize_bgp_mode(
        mode if isinstance(mode, str) or mode is None else mode
    )
    if rpki and rtbh:
        raise BgpConfigError(
            "RPKI and RTBH profiles are mutually exclusive.",
            topology=isp_plan.topology_name,
        )
    if rpki and resolved != "ebgp":
        raise BgpConfigError(
            f"RPKI capability requires bgp_mode 'ebgp' (got {resolved!r}).",
            topology=isp_plan.topology_name,
        )
    if rtbh and resolved != "ebgp":
        raise BgpConfigError(
            f"RTBH capability requires bgp_mode 'ebgp' (got {resolved!r}).",
            topology=isp_plan.topology_name,
        )
    if resolved == "none":
        return None
    if not isp_plan.nodes:
        raise BgpCompileError(
            "Cannot build BGP for an empty ISP plan.",
            topology=isp_plan.topology_name,
        )
    if resolved == "ibgp_rr":
        return _compile_ibgp_rr(isp_plan)
    if resolved == "ebgp":
        plan = _compile_ebgp(isp_plan, mode=resolved)
        if rpki:
            from nika.net_env.isp.bgp.rpki_profile import apply_rpki_profile

            return apply_rpki_profile(plan)
        if rtbh:
            from nika.net_env.isp.bgp.rtbh_profile import apply_rtbh_profile

            return apply_rtbh_profile(plan)
        return plan
    raise BgpConfigError(f"Unsupported bgp_mode {resolved!r}.")


def _devices(isp_plan: IspPlan) -> list[str]:
    return sorted(node.device_name for node in isp_plan.nodes)


def _loopbacks(isp_plan: IspPlan) -> dict[str, str]:
    return {node.device_name: node.loopback for node in isp_plan.nodes}


def _compile_ibgp_rr(isp_plan: IspPlan) -> BgpPlan:
    devices = _devices(isp_plan)
    loopbacks = _loopbacks(isp_plan)
    n = len(devices)
    rr_n = min(2, n)
    rrs = devices[:rr_n]
    clients = devices[rr_n:]
    cluster_id = loopbacks[rrs[0]]

    # Edge originators: last min(3, n) devices; one /24 each (lab TEST-NET range).
    edge_n = min(3, n)
    edges = devices[-edge_n:]
    originated: list[BgpOriginatedPrefix] = []
    for index, device in enumerate(edges):
        # 203.0.113.0/24, 203.0.114.0/24, 203.0.115.0/24
        prefix = IPv4Network(f"203.0.{113 + index}.0/24")
        ping = str(prefix.network_address + 1)
        originated.append(
            BgpOriginatedPrefix(device=device, prefix=str(prefix), ping_address=ping)
        )

    sessions: list[BgpSession] = []
    # RR mesh
    for i, left in enumerate(rrs):
        for right in rrs[i + 1 :]:
            sessions.append(
                BgpSession(
                    local_device=left,
                    remote_device=right,
                    local_ip=loopbacks[left],
                    remote_ip=loopbacks[right],
                    local_asn=IBGP_ASN,
                    remote_asn=IBGP_ASN,
                    session_type="ibgp",
                    update_source="lo",
                )
            )
            sessions.append(
                BgpSession(
                    local_device=right,
                    remote_device=left,
                    local_ip=loopbacks[right],
                    remote_ip=loopbacks[left],
                    local_asn=IBGP_ASN,
                    remote_asn=IBGP_ASN,
                    session_type="ibgp",
                    update_source="lo",
                )
            )
    # Client → each RR (and RR marks client)
    for client in clients:
        for rr in rrs:
            sessions.append(
                BgpSession(
                    local_device=client,
                    remote_device=rr,
                    local_ip=loopbacks[client],
                    remote_ip=loopbacks[rr],
                    local_asn=IBGP_ASN,
                    remote_asn=IBGP_ASN,
                    session_type="ibgp",
                    update_source="lo",
                )
            )
            sessions.append(
                BgpSession(
                    local_device=rr,
                    remote_device=client,
                    local_ip=loopbacks[rr],
                    remote_ip=loopbacks[client],
                    local_asn=IBGP_ASN,
                    remote_asn=IBGP_ASN,
                    session_type="ibgp",
                    update_source="lo",
                    route_reflector_client=True,
                )
            )

    sessions_t = tuple(sessions)
    originated_t = tuple(originated)
    by_dev_sessions: dict[str, list[BgpSession]] = {d: [] for d in devices}
    for sess in sessions_t:
        by_dev_sessions[sess.local_device].append(sess)
    by_dev_orig: dict[str, list[BgpOriginatedPrefix]] = {d: [] for d in devices}
    for pref in originated_t:
        by_dev_orig[pref.device].append(pref)

    nodes: list[BgpNodePlan] = []
    for device in devices:
        roles: list[str] = []
        if device in rrs:
            roles.append("rr")
        if device in clients:
            roles.append("client")
        if by_dev_orig[device]:
            roles.append("originator")
        nodes.append(
            BgpNodePlan(
                device_name=device,
                asn=IBGP_ASN,
                roles=tuple(roles) or ("member",),
                router_id=loopbacks[device],
                sessions=tuple(by_dev_sessions[device]),
                originated=tuple(by_dev_orig[device]),
                cluster_id=cluster_id if device in rrs else None,
            )
        )

    # Observers: a non-originator RR client (or RR) should see each originated prefix.
    observers = [d for d in devices if d not in {o.device for o in originated_t}]
    if not observers:
        observers = devices[:1]
    expect: list[tuple[str, str]] = []
    for pref in originated_t:
        observer = next((o for o in observers if o != pref.device), observers[0])
        expect.append((observer, pref.prefix))

    inventory = _inventory(
        mode="ibgp_rr",
        topology_name=isp_plan.topology_name,
        nodes=nodes,
        sessions=sessions_t,
        originated=originated_t,
        expect_reachable=tuple(expect),
        deny_prefixes=("10.0.0.0/8", "10.255.0.0/16"),
    )
    return BgpPlan(
        mode="ibgp_rr",
        topology_name=isp_plan.topology_name,
        nodes=tuple(nodes),
        sessions=sessions_t,
        originated=originated_t,
        expect_reachable=tuple(expect),
        deny_prefixes=("10.0.0.0/8", "10.255.0.0/16"),
        inventory=inventory,
    )


def _connected_as_members(isp_plan: IspPlan, count: int) -> dict[int, list[str]]:
    """Partition a connected topology into deterministic connected AS regions."""
    devices = _devices(isp_plan)
    graph: dict[str, list[str]] = {device: [] for device in devices}
    for link in isp_plan.links:
        graph[link.endpoint_a].append(link.endpoint_b)
        graph[link.endpoint_b].append(link.endpoint_a)
    for neighbors in graph.values():
        neighbors.sort()

    def distances(source: str) -> dict[str, int]:
        result = {source: 0}
        queue = [source]
        for node in queue:
            for neighbor in graph[node]:
                if neighbor not in result:
                    result[neighbor] = result[node] + 1
                    queue.append(neighbor)
        return result

    first_distances = distances(devices[0])
    if len(first_distances) != len(devices):
        raise BgpCompileError(
            "eBGP preset requires a connected ISP topology.",
            topology=isp_plan.topology_name,
        )

    seeds = [devices[0]]
    distance_cache = {devices[0]: first_distances}
    while len(seeds) < count:
        candidates = []
        for device in devices:
            if device in seeds:
                continue
            nearest = min(distance_cache[seed][device] for seed in seeds)
            candidates.append((-nearest, device))
        _, seed = min(candidates)
        seeds.append(seed)
        distance_cache[seed] = distances(seed)

    # Multi-source expansion records a same-AS predecessor for every assigned
    # node, so each resulting region is connected by construction.
    frontier: list[tuple[int, int, str]] = []
    for index, seed in enumerate(seeds):
        heapq.heappush(frontier, (0, index, seed))
    owner: dict[str, int] = {}
    while frontier:
        distance, seed_index, device = heapq.heappop(frontier)
        if device in owner:
            continue
        owner[device] = seed_index
        for neighbor in graph[device]:
            if neighbor not in owner:
                heapq.heappush(frontier, (distance + 1, seed_index, neighbor))

    return {
        EBGP_BASE_ASN + index: sorted(
            device for device, assigned in owner.items() if assigned == index
        )
        for index in range(count)
    }


def _compile_ebgp(
    isp_plan: IspPlan,
    *,
    mode: IspBgpMode = "ebgp",
    members: dict[int, list[str]] | None = None,
) -> BgpPlan:
    devices = _devices(isp_plan)
    loopbacks = _loopbacks(isp_plan)
    n = len(devices)
    k = min(3, n)
    members = members or _connected_as_members(isp_plan, k)
    assigned = sorted(device for group in members.values() for device in group)
    if assigned != devices:
        raise BgpCompileError(
            "eBGP AS membership must assign every device exactly once.",
            topology=isp_plan.topology_name,
        )
    asn_of: dict[str, int] = {}
    for asn, group in sorted(members.items()):
        for device in group:
            asn_of[device] = asn

    sessions: list[BgpSession] = []
    border: set[str] = set()
    for link in isp_plan.links:
        a, b = link.endpoint_a, link.endpoint_b
        if asn_of[a] == asn_of[b]:
            continue
        border.add(a)
        border.add(b)
        sessions.append(
            BgpSession(
                local_device=a,
                remote_device=b,
                local_ip=link.address_a,
                remote_ip=link.address_b,
                local_asn=asn_of[a],
                remote_asn=asn_of[b],
                session_type="ebgp",
                update_source=None,
            )
        )
        sessions.append(
            BgpSession(
                local_device=b,
                remote_device=a,
                local_ip=link.address_b,
                remote_ip=link.address_a,
                local_asn=asn_of[b],
                remote_asn=asn_of[a],
                session_type="ebgp",
                update_source=None,
            )
        )

    # One deterministic route reflector per AS keeps session growth linear.
    route_reflectors: dict[int, str] = {}
    for asn, group in sorted(members.items()):
        ordered = sorted(group)
        reflector = ordered[0]
        route_reflectors[asn] = reflector
        for client in ordered[1:]:
            sessions.extend(
                (
                    BgpSession(
                        local_device=client,
                        remote_device=reflector,
                        local_ip=loopbacks[client],
                        remote_ip=loopbacks[reflector],
                        local_asn=asn,
                        remote_asn=asn,
                        session_type="ibgp",
                        update_source="lo",
                    ),
                    BgpSession(
                        local_device=reflector,
                        remote_device=client,
                        local_ip=loopbacks[reflector],
                        remote_ip=loopbacks[client],
                        local_asn=asn,
                        remote_asn=asn,
                        session_type="ibgp",
                        update_source="lo",
                        route_reflector_client=True,
                    ),
                )
            )
    # Originate on last border in each AS; fallback to last member.
    # One /24 per AS: 198.51.100.0/24, 198.51.101.0/24, 198.51.102.0/24.
    originated: list[BgpOriginatedPrefix] = []
    asn_list = sorted(members)
    for index, asn in enumerate(asn_list):
        group = members[asn]
        borders_in = sorted(d for d in group if d in border)
        origin = borders_in[-1] if borders_in else group[-1]
        # Build /24 as 198.51.(100+index).0/24
        prefix = IPv4Network(f"198.51.{100 + index}.0/24")
        ping = str(prefix.network_address + 1)
        originated.append(
            BgpOriginatedPrefix(device=origin, prefix=str(prefix), ping_address=ping)
        )

    sessions_t = tuple(sessions)
    originated_t = tuple(originated)
    by_dev_sessions: dict[str, list[BgpSession]] = {d: [] for d in devices}
    for sess in sessions_t:
        by_dev_sessions[sess.local_device].append(sess)
    by_dev_orig: dict[str, list[BgpOriginatedPrefix]] = {d: [] for d in devices}
    for pref in originated_t:
        by_dev_orig[pref.device].append(pref)

    nodes: list[BgpNodePlan] = []
    for device in devices:
        roles: list[str] = ["member"]
        reflector = route_reflectors[asn_of[device]]
        roles.append("rr" if device == reflector else "client")
        if device in border:
            roles.append("border")
        if by_dev_orig[device]:
            roles.append("originator")
        nodes.append(
            BgpNodePlan(
                device_name=device,
                asn=asn_of[device],
                roles=tuple(roles),
                router_id=loopbacks[device],
                sessions=tuple(by_dev_sessions[device]),
                originated=tuple(by_dev_orig[device]),
                cluster_id=loopbacks[device] if device == reflector else None,
            )
        )

    # A direct eBGP peer provides a deterministic cross-AS baseline target.
    expect: list[tuple[str, str]] = []
    sessions_by_local: dict[str, list[BgpSession]] = {d: [] for d in devices}
    for sess in sessions_t:
        sessions_by_local[sess.local_device].append(sess)
    for pref in originated_t:
        peers = sorted(
            (
                session
                for session in sessions_by_local.get(pref.device, [])
                if session.session_type == "ebgp"
            ),
            key=lambda s: s.remote_device,
        )
        if peers:
            expect.append((peers[0].remote_device, pref.prefix))

    if k > 1 and not border:
        raise BgpCompileError(
            "eBGP preset found no cross-AS ISP links for this partition; "
            "cannot build eBGP sessions.",
            topology=isp_plan.topology_name,
        )

    inventory = _inventory(
        mode=mode,
        topology_name=isp_plan.topology_name,
        nodes=nodes,
        sessions=sessions_t,
        originated=originated_t,
        expect_reachable=tuple(expect),
        deny_prefixes=("10.0.0.0/8", "10.255.0.0/16"),
    )
    return BgpPlan(
        mode=mode,
        topology_name=isp_plan.topology_name,
        nodes=tuple(nodes),
        sessions=sessions_t,
        originated=originated_t,
        expect_reachable=tuple(expect),
        deny_prefixes=("10.0.0.0/8", "10.255.0.0/16"),
        inventory=inventory,
    )


def scope_igp_to_bgp_as(isp_plan: IspPlan, bgp_plan: BgpPlan | None) -> IspPlan:
    """Make AS-boundary links passive in the IGP and re-render its config."""
    if bgp_plan is None or bgp_plan.mode != "ebgp":
        return isp_plan
    asn_of = {node.device_name: node.asn for node in bgp_plan.nodes}
    scoped_nodes: list[PlannedNode] = []
    boundary_links: set[str] = set()
    for node in isp_plan.nodes:
        interfaces = []
        for interface in node.interfaces:
            boundary = asn_of[node.device_name] != asn_of[interface.peer_device]
            if boundary:
                boundary_links.add(interface.link_id)
            interfaces.append(replace(interface, passive=interface.passive or boundary))
        interfaces_t = tuple(interfaces)
        draft = replace(node, interfaces=interfaces_t, frr_conf="")
        scoped_nodes.append(
            replace(
                draft,
                frr_conf=render_frr_conf(
                    draft, igp=isp_plan.igp, interfaces=interfaces_t
                ),
            )
        )
    nodes_t = tuple(scoped_nodes)
    inventory = build_inventory(
        topology_name=isp_plan.topology_name,
        igp=isp_plan.igp,
        metric_strategy=isp_plan.metric_strategy,
        constant_metric=isp_plan.constant_metric,
        nodes=nodes_t,
        links=isp_plan.links,
    )
    inventory["igp_scope"] = "per_as"
    inventory["igp_passive_boundary_links"] = sorted(boundary_links)
    return replace(isp_plan, nodes=nodes_t, inventory=inventory)


def _inventory(
    *,
    mode: str,
    topology_name: str,
    nodes: list[BgpNodePlan],
    sessions: tuple[BgpSession, ...],
    originated: tuple[BgpOriginatedPrefix, ...],
    expect_reachable: tuple[tuple[str, str], ...],
    deny_prefixes: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "bgp_mode": mode,
        "topology_name": topology_name,
        "nodes": [
            {
                "device": n.device_name,
                "asn": n.asn,
                "roles": list(n.roles),
                "router_id": n.router_id,
                "cluster_id": n.cluster_id,
                "session_count": len(n.sessions),
                "originated": [o.prefix for o in n.originated],
            }
            for n in sorted(nodes, key=lambda item: item.device_name)
        ],
        "sessions": [
            {
                "local_device": s.local_device,
                "remote_device": s.remote_device,
                "local_ip": s.local_ip,
                "remote_ip": s.remote_ip,
                "local_asn": s.local_asn,
                "remote_asn": s.remote_asn,
                "session_type": s.session_type,
                "update_source": s.update_source,
                "route_reflector_client": s.route_reflector_client,
            }
            for s in sorted(
                sessions,
                key=lambda item: (
                    item.local_device,
                    item.remote_device,
                    item.remote_ip,
                ),
            )
        ],
        "originated": [
            {
                "device": o.device,
                "prefix": o.prefix,
                "ping_address": o.ping_address,
            }
            for o in sorted(originated, key=lambda item: (item.device, item.prefix))
        ],
        "expect_reachable": [
            {"observer": obs, "prefix": pref} for obs, pref in sorted(expect_reachable)
        ],
        "deny_prefixes": list(deny_prefixes),
    }
