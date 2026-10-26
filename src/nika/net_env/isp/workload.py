"""Business traffic of the SNDlib ISP labs (always on, every case).

Every PoP's edge stub stands for the customers attached there. The flows
follow the topology's SNDlib demand matrix, restricted to stub pairs the
design lets communicate (one AS in ``ebgp`` mode):

- **Bulk transfers.** A few stubs (one per ``BULK_POPS_PER_FLOW`` PoPs) pull
  the data set, published over HTTP (port 8080), of the PoP they have the
  largest demand from, picking the largest-demand pairs. Each transfer is
  paced at a rate scaled from its demand, so the links it crosses carry steady
  TCP load without saturating anything. Every lab link is a userspace switch
  that costs CPU per packet, so more transfers would slow the whole host.
- **Long-lived sessions.** In BGP labs, each business prefix hosts a web/push
  service on its originator's service address, and the stubs with the largest
  demand toward that PoP keep persistent sessions and page loads to it. Without
  BGP (or on routers that cannot run the service) the busiest PoP's stub hosts
  it instead.

- **Customer site.** In plain eBGP labs, one customer site behind a border
  router keeps sessions to the web service of a neighboring AS.

- **Routine changes.** On FRR routers, a ``change-window`` process makes small
  benign operator edits (an interface description, an unused reference
  prefix-list) at start and every few minutes, so mgmtd's commit history shows
  ordinary activity in healthy and fault cases alike.

The load stays well below what the lab links forward, so a healthy lab shows
no loss or queueing.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import TYPE_CHECKING

from nika.net_env.utils.rack_workload import seed_for
from nika.traffic.workload import (
    BULK_DOWNLOAD,
    BULK_TRANSIT,
    BULK_TRANSIT_MIN_KBPS,
    BusinessWorkload,
    WorkloadExercise,
    WorkloadProcess,
    routine_change_processes,
)

if TYPE_CHECKING:
    from nika.net_env.isp.bgp.plan import BgpPlan
    from nika.net_env.isp.igp.plan import IspPlan
    from nika.net_env.isp.traffic.stubs import IspTrafficAttachment

BULK_PORT = 8080
BULK_ROOT = "/var/www"
BULK_OBJECT = "large.bin"
BULK_OBJECT_SIZE = "8M"
# One transfer per this many PoPs: the userspace switches that carry every
# lab link spend CPU per forwarded packet, so only a few transfers run.
BULK_POPS_PER_FLOW = 6
# Per-transfer pacing: the median demand gets BULK_MEDIAN_KBPS.
BULK_MEDIAN_KBPS = 1600
BULK_MIN_KBPS = BULK_TRANSIT_MIN_KBPS
BULK_MAX_KBPS = 2000
SERVICE_PORT = 80
SESSION_CLIENTS = 3
SESSION_ARGS = (
    "--sessions", "4", "--lifetime", "120-600",
    "--burst", "3", "--burst-every", "20-40", "--burst-kb", "8-64",
)  # fmt: skip

# Stub startup line that creates the published data set (sparse file).
BULK_OBJECT_STARTUP = (
    f"mkdir -p {BULK_ROOT} && truncate -s {BULK_OBJECT_SIZE} {BULK_ROOT}/{BULK_OBJECT}"
)


@dataclass(frozen=True)
class BulkFlow:
    """``client`` pulls the data set of ``server`` (both router devices)."""

    server: str
    client: str
    kbps: int


def demand_by_device(topology: str | Path, plan: "IspPlan") -> dict:
    """SNDlib demand values keyed by ``(src_device, dst_device)``."""
    from nika.net_env.isp.traffic.resolve import series_from_demands
    from nika.topology import load_topology

    device_of = {node.node_id: node.device_name for node in plan.nodes}
    series = series_from_demands(load_topology(topology))
    demand: dict[tuple[str, str], float] = defaultdict(float)
    for flow in series.intervals[0].flows:
        src = device_of.get(flow.src_node_id)
        dst = device_of.get(flow.dst_node_id)
        if src and dst and src != dst:
            demand[(src, dst)] += flow.rate
    return dict(demand)


def reachability_groups(plan: "IspPlan", bgp_plan: "BgpPlan | None") -> list[list[str]]:
    """Router groups whose stubs may talk: one per AS under eBGP, else one."""
    devices = sorted(node.device_name for node in plan.nodes)
    if bgp_plan is None or bgp_plan.mode != "ebgp":
        return [devices]
    by_asn: dict[int, list[str]] = defaultdict(list)
    for node in bgp_plan.nodes:
        by_asn[node.asn].append(node.device_name)
    return [sorted(group) for _asn, group in sorted(by_asn.items())]


def select_bulk_flows(
    groups: Iterable[list[str]], demand: Mapping[tuple[str, str], float]
) -> list[BulkFlow]:
    """The largest-demand transfers: one per ``BULK_POPS_PER_FLOW`` PoPs.

    Each PoP's best source is its candidate; the top-demand candidates of a
    group run. Rates scale with demand inside ``BULK_MIN_KBPS`` to
    ``BULK_MAX_KBPS``, so every link a transfer crosses carries at least
    ``BULK_TRANSIT_MIN_KBPS``.
    """
    pairs: dict[tuple[str, str], float] = {}
    for group in groups:
        if len(group) < 2:
            continue
        candidates: dict[tuple[str, str], float] = {}
        for client in group:
            server = max(
                (d for d in group if d != client),
                key=lambda s: (demand.get((s, client), 0.0), s),
            )
            candidates[(server, client)] = demand.get((server, client), 0.0)
        keep = max(1, -(-len(group) // BULK_POPS_PER_FLOW))
        ranked = sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0]))
        pairs.update(ranked[:keep])
    positive = [w for w in pairs.values() if w > 0]
    reference = median(positive) if positive else 1.0
    flows = []
    for (server, client), weight in sorted(pairs.items()):
        scaled = BULK_MEDIAN_KBPS * (weight / reference if weight > 0 else 0.5)
        kbps = int(min(BULK_MAX_KBPS, max(BULK_MIN_KBPS, round(scaled, -2))))
        flows.append(BulkFlow(server=server, client=client, kbps=kbps))
    return flows


def igp_unique_path(
    plan: "IspPlan", groups: list[list[str]], src: str, dst: str
) -> list[tuple[str, str]] | None:
    """Egress ``(router, iface)`` hops of the single IGP shortest path, or None.

    The IGP floods only inside a group (AS-boundary links are passive), so
    paths stay inside it. Equal-cost alternatives make the path ``None``:
    which one a flow hashes onto is not known in advance.
    """
    group = next((g for g in groups if src in g and dst in g), None)
    if group is None:
        return None
    members = set(group)
    edges: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for node in plan.nodes:
        if node.device_name not in members:
            continue
        for iface in node.interfaces:
            if iface.passive or iface.peer_device not in members:
                continue
            edges[node.device_name].append(
                (iface.peer_device, iface.name, iface.metric)
            )
    dist = {src: 0}
    count = {src: 1}
    prev: dict[str, tuple[str, str]] = {}
    queue = [(0, src)]
    while queue:
        cost, node = heapq.heappop(queue)
        if cost > dist[node]:
            continue
        for peer, iface, metric in edges[node]:
            candidate = cost + metric
            if candidate < dist.get(peer, candidate + 1):
                dist[peer] = candidate
                count[peer] = count[node]
                prev[peer] = (node, iface)
                heapq.heappush(queue, (candidate, peer))
            elif candidate == dist[peer]:
                count[peer] += count[node]
    if dst not in dist or count[dst] != 1:
        return None
    hops: list[tuple[str, str]] = []
    node = dst
    while node != src:
        hops.append(prev[node])
        node = prev[node][0]
    return list(reversed(hops))


def _service_sites(
    plan: "IspPlan",
    groups: list[list[str]],
    bgp_plan: "BgpPlan | None",
    demand: Mapping[tuple[str, str], float],
    router_services: bool,
) -> list[tuple[str, str, str]]:
    """``(server_node, address, pop_device)`` of each long-lived service."""
    sites: dict[str, tuple[str, str, str]] = {}
    if bgp_plan is not None and router_services:
        internal = set(bgp_plan.inventory.get("leak_prefixes") or ())
        for pref in sorted(bgp_plan.originated, key=lambda p: p.prefix):
            if pref.prefix not in internal:
                sites.setdefault(
                    pref.device, (pref.device, pref.ping_address, pref.device)
                )
        if sites:
            return list(sites.values())
    hubs: list[tuple[str, str, str]] = []
    for group in groups:
        if len(group) < 2:
            continue
        hub = max(
            group,
            key=lambda d: (sum(demand.get((s, d), 0.0) for s in group), d),
        )
        hubs.append(("", "", hub))
    return hubs


def build_isp_workload(
    plan: "IspPlan",
    traffic: "IspTrafficAttachment",
    bgp_plan: "BgpPlan | None",
    topology: str | Path,
    *,
    router_services: bool,
    router_iface: Callable[[str], str] = str,
    routine_changes: bool = False,
) -> BusinessWorkload:
    """Workload of one ISP lab.

    ``router_services`` lets routers host the business services;
    ``router_iface`` maps plan interface names to the runtime's names;
    ``routine_changes`` makes every FRR router apply benign operator edits
    now and then, so its mgmtd commit history is not empty of other activity.
    """
    stub_of = {host.router_device: host for host in traffic.hosts}
    groups = reachability_groups(plan, bgp_plan)
    groups = [[d for d in g if d in stub_of] for g in groups]
    demand = demand_by_device(topology, plan)
    flows = select_bulk_flows(groups, demand)

    processes: list[WorkloadProcess] = []
    exercises: list[WorkloadExercise] = []
    for device in sorted({f.server for f in flows}):
        processes.append(
            WorkloadProcess(
                node=stub_of[device].host_name,
                app="file_server",
                command="filedist",
                args=("--root", BULK_ROOT, "--port", str(BULK_PORT)),
            )
        )
    by_client: dict[str, list[BulkFlow]] = defaultdict(list)
    for flow in flows:
        by_client[flow.client].append(flow)
    transit: dict[tuple[str, str], int] = defaultdict(int)
    for client, pulls in sorted(by_client.items()):
        host = stub_of[client]
        urls = [
            f"http://{stub_of[f.server].address}:{BULK_PORT}/{BULK_OBJECT}"
            f"?rate={f.kbps}k"
            for f in pulls
        ]
        processes.append(
            WorkloadProcess(
                node=host.host_name,
                app="file_sync",
                command="filesync",
                args=(
                    *urls,
                    "--pause",
                    "1-3",
                    "--parallel",
                    "--seed",
                    seed_for(client),
                ),
            )
        )
        exercises.append(WorkloadExercise(BULK_DOWNLOAD, (host.host_name,)))
        for flow in pulls:
            server = stub_of[flow.server]
            transit[(server.host_name, server.host_iface)] += flow.kbps
            edge = next(e for e in traffic.edge_links if e.router_device == flow.client)
            transit[(flow.client, edge.router_iface)] += flow.kbps
            path = igp_unique_path(plan, groups, flow.server, flow.client)
            for router, iface in path or ():
                transit[(router, router_iface(iface))] += flow.kbps
    for target, kbps in sorted(transit.items()):
        if kbps >= BULK_TRANSIT_MIN_KBPS:
            exercises.append(WorkloadExercise(BULK_TRANSIT, target))

    sites = _service_sites(plan, groups, bgp_plan, demand, router_services)
    for node, address, pop in sites:
        group = next(g for g in groups if pop in g)
        if not node:
            node, address = stub_of[pop].host_name, stub_of[pop].address
        clients = sorted(
            (d for d in group if d != pop),
            key=lambda d: (-demand.get((d, pop), 0.0), d),
        )[:SESSION_CLIENTS]
        processes.append(
            WorkloadProcess(
                node=node,
                app="web_service",
                command="portal",
                args=("--port", str(SERVICE_PORT), "--seed", seed_for(node)),
            )
        )
        for client in clients:
            host = stub_of[client].host_name
            processes.append(
                WorkloadProcess(
                    node=host,
                    app="web_client",
                    command=f"webclient-{pop}",
                    args=(address, *SESSION_ARGS, "--seed", seed_for(host + pop)),
                )
            )
    site = bgp_plan.customer_site if bgp_plan is not None else None
    service_pops = {address: pop for _node, address, pop in sites if address}
    if site is not None and site.service_address in service_pops:
        pop = service_pops[site.service_address]
        processes.append(
            WorkloadProcess(
                node=site.host_name,
                app="web_client",
                command=f"webclient-{pop}",
                args=(
                    site.service_address,
                    *SESSION_ARGS,
                    "--seed",
                    seed_for(site.host_name + pop),
                ),
            )
        )
    if routine_changes:
        processes += routine_change_processes(n.device_name for n in plan.nodes)
    return BusinessWorkload(processes=tuple(processes), exercises=tuple(exercises))
