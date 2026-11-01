"""Which business prefixes another router can hijack with a visible victim.

Inside one AS, every router prefers the origin with the lower IGP cost to its
next hop (a route reflector reflects only its own best path; on equal cost
the lower originator router ID is best, and iBGP multipath may also spread
flows over both). A router that also originates a business prefix draws the
traffic of every router whose choice flips to it. The hijack has a visible
victim when that includes a router whose customers hold sessions to the
prefix's service.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nika.net_env.isp.bgp.plan import BgpPlan
    from nika.net_env.isp.igp.plan import IspPlan
    from nika.net_env.isp.traffic.stubs import IspTrafficAttachment
    from nika.traffic.workload import BusinessWorkload


@dataclass(frozen=True)
class HijackTarget:
    """``hijacker`` originating ``prefix`` diverts ``observer``'s traffic."""

    hijacker: str
    prefix: str
    observer: str
    observer_host: str
    probe_ip: str


def _igp_costs(plan: "IspPlan", members: set[str], src: str) -> dict[str, int]:
    edges: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for node in plan.nodes:
        for iface in node.interfaces:
            if not iface.passive and iface.peer_device in members:
                edges[node.device_name].append((iface.peer_device, iface.metric))
    dist = {src: 0}
    queue = [(0, src)]
    while queue:
        cost, node = heapq.heappop(queue)
        if cost > dist[node]:
            continue
        for peer, metric in edges[node]:
            if cost + metric < dist.get(peer, cost + metric + 1):
                dist[peer] = cost + metric
                heapq.heappush(queue, (cost + metric, peer))
    return dist


def best_origin(
    router: str,
    origins: list[str],
    reflectors: list[str],
    cost: Mapping[str, Mapping[str, int]],
    router_id: Mapping[str, str],
) -> str:
    """Origin whose path ``router`` installs for a prefix several routers originate."""
    if router in origins:
        return router

    def pick(at: str, candidates: set[str]) -> str:
        return min(
            candidates,
            key=lambda o: (cost[at].get(o, 1 << 30), IPv4Address(router_id[o])),
        )

    if router in reflectors:
        return pick(router, set(origins))
    # A client receives each reflector's best path (an originating reflector
    # sends its own).
    received = {r if r in origins else pick(r, set(origins)) for r in reflectors}
    return pick(router, received)


def hijack_targets(
    plan: "IspPlan",
    bgp_plan: "BgpPlan",
    stub_of: Mapping[str, str],
    session_clients: Mapping[str, set[str]],
) -> list[HijackTarget]:
    """Hijacks of a business prefix that divert a service client's traffic.

    ``stub_of`` maps routers to their edge stub; ``session_clients`` maps a
    prefix to the routers whose stubs hold sessions to its service.
    """
    asn_of = {n.device_name: n.asn for n in bgp_plan.nodes}
    router_id = {n.device_name: n.router_id for n in bgp_plan.nodes}
    reflectors_of: dict[int, list[str]] = defaultdict(list)
    for node in bgp_plan.nodes:
        if "rr" in node.roles:
            reflectors_of[node.asn].append(node.device_name)
    targets: list[HijackTarget] = []
    for pref in sorted(bgp_plan.originated, key=lambda p: (p.prefix, p.device)):
        clients = session_clients.get(pref.prefix) or set()
        asn = asn_of[pref.device]
        members = {d for d, a in asn_of.items() if a == asn}
        cost = {r: _igp_costs(plan, members, r) for r in members}
        for hijacker in sorted(members & set(stub_of)):
            if hijacker == pref.device:
                continue
            origins = [pref.device, hijacker]
            victims = sorted(
                c
                for c in clients
                if c != hijacker
                and best_origin(c, origins, reflectors_of[asn], cost, router_id)
                == hijacker
            )
            if victims:
                targets.append(
                    HijackTarget(
                        hijacker=hijacker,
                        prefix=pref.prefix,
                        observer=victims[0],
                        observer_host=stub_of[victims[0]],
                        probe_ip=pref.ping_address,
                    )
                )
    return targets


def lab_hijack_targets(
    plan: "IspPlan",
    bgp_plan: "BgpPlan | None",
    traffic: "IspTrafficAttachment",
    workload: "BusinessWorkload | None",
) -> list[HijackTarget]:
    """Hijack targets of a deployed ISP lab.

    With ``workload``, a prefix's clients are the routers whose stubs hold
    sessions to its service: the originated ping address on the router, or the
    originator's stub address when the service runs on the stub. Without a
    workload, every router with a stub is a client of every business prefix.
    """
    if bgp_plan is None:
        return []
    stub_of = {h.router_device: h.host_name for h in traffic.hosts}
    if workload is None:
        clients = {o.prefix: set(stub_of) for o in bgp_plan.originated}
        return hijack_targets(plan, bgp_plan, stub_of, clients)
    router_of = {stub: router for router, stub in stub_of.items()}
    prefix_at = {o.ping_address: o.prefix for o in bgp_plan.originated}
    address_of = {h.host_name: h.address for h in traffic.hosts}
    for origin in bgp_plan.originated:
        stub = stub_of.get(origin.device)
        if stub is not None:
            prefix_at.setdefault(address_of[stub], origin.prefix)
    clients = {}
    for process in workload.processes:
        if (
            process.app == "web_client"
            and process.node in router_of
            and process.args[0] in prefix_at
        ):
            clients.setdefault(prefix_at[process.args[0]], set()).add(
                router_of[process.node]
            )
    return hijack_targets(plan, bgp_plan, stub_of, clients)
