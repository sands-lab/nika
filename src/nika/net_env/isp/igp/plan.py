"""Compile SNDlib NetworkTopology into a stable ISP plan."""

from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from typing import Any

from nika.net_env.isp.igp.config import IspConfig
from nika.net_env.isp.igp.errors import IspCompileError, IspConfigError
from nika.net_env.isp.igp.frr import render_frr_conf
from nika.topology import (
    NetworkTopology,
    TopoLink,
    link_preinstalled_capacity,
    load_sndlib_topology,
)

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


@dataclass(frozen=True)
class PlannedInterface:
    name: str
    link_id: str
    collision_domain: str
    peer_device: str
    peer_address: str
    address: str
    prefixlen: int
    subnet: str
    metric: int
    passive: bool = False


@dataclass(frozen=True)
class PlannedNode:
    node_id: str
    device_name: str
    loopback: str
    router_id: str
    interfaces: tuple[PlannedInterface, ...]
    startup_commands: tuple[str, ...]
    frr_conf: str


@dataclass(frozen=True)
class PlannedLink:
    link_id: str
    collision_domain: str
    source_node_id: str
    target_node_id: str
    endpoint_a: str
    endpoint_b: str
    iface_a: str
    iface_b: str
    address_a: str
    address_b: str
    subnet: str
    metric: int


@dataclass(frozen=True)
class IspPlan:
    topology_name: str
    igp: str
    metric_strategy: str
    constant_metric: int
    nodes: tuple[PlannedNode, ...]
    links: tuple[PlannedLink, ...]
    inventory: dict[str, Any]


def active_igp_links(plan: IspPlan) -> tuple[PlannedLink, ...]:
    """Return backbone links whose two router interfaces run the IGP."""
    active_endpoints: dict[str, int] = {}
    backbone = {link.link_id for link in plan.links}
    for node in plan.nodes:
        for interface in node.interfaces:
            if interface.link_id in backbone and not interface.passive:
                active_endpoints[interface.link_id] = (
                    active_endpoints.get(interface.link_id, 0) + 1
                )
    return tuple(link for link in plan.links if active_endpoints.get(link.link_id) == 2)


def igp_components(plan: IspPlan) -> tuple[tuple[str, ...], ...]:
    """Return deterministic connected components of the active IGP graph."""
    graph: dict[str, set[str]] = {node.device_name: set() for node in plan.nodes}
    for link in active_igp_links(plan):
        graph[link.endpoint_a].add(link.endpoint_b)
        graph[link.endpoint_b].add(link.endpoint_a)
    remaining = set(graph)
    components: list[tuple[str, ...]] = []
    while remaining:
        root = min(remaining)
        reached = {root}
        queue = [root]
        for device in queue:
            for peer in sorted(graph[device]):
                if peer not in reached:
                    reached.add(peer)
                    queue.append(peer)
        remaining -= reached
        components.append(tuple(sorted(reached)))
    return tuple(components)


def slugify(raw: str, *, kind: str) -> str:
    """Map an SNDlib id to a Kathara-safe runtime name."""
    slug = _SLUG_RE.sub("_", raw.strip().lower()).strip("_")
    if not slug:
        raise IspCompileError(f"Cannot derive runtime {kind} name from {raw!r}.")
    if slug[0].isdigit():
        slug = f"{kind[0]}_{slug}"
    if not _SAFE_NAME_RE.match(slug):
        raise IspCompileError(
            f"Runtime {kind} name {slug!r} from {raw!r} is not Kathara-safe."
        )
    return slug


def link_metric(link: TopoLink, config: IspConfig) -> int:
    """Resolve IGP metric for one link according to the configured strategy."""
    if config.metric_strategy == "constant":
        return config.constant_metric
    if config.metric_strategy == "routing_cost":
        if link.routing_cost is None:
            return config.constant_metric
        value = int(round(link.routing_cost))
        if value < 1:
            return config.constant_metric
        return value
    if config.metric_strategy == "inv_capacity":
        capacity = link_preinstalled_capacity(link)
        if capacity is None or capacity <= 0:
            return config.constant_metric
        value = int(round(config.inv_capacity_reference / capacity))
        return max(1, value)
    raise IspConfigError(f"Unsupported metric strategy {config.metric_strategy!r}.")


def compile_isp_plan(
    config: IspConfig | None = None,
    *,
    topology: NetworkTopology | None = None,
) -> IspPlan:
    """Compile an ISP plan from config and/or an explicit NetworkTopology."""
    cfg = (config or IspConfig()).validated()
    topo = topology if topology is not None else load_sndlib_topology(cfg.topology)

    nodes_sorted = tuple(sorted(topo.nodes, key=lambda n: n.id))
    links_sorted = tuple(sorted(topo.links, key=lambda link: link.id))

    device_by_node = _assign_device_names(nodes_sorted, topology_name=topo.name)
    loopbacks = _assign_loopbacks(
        nodes_sorted, pool=cfg.loopback_pool, topology_name=topo.name
    )
    link_addrs = _assign_link_addresses(
        links_sorted,
        device_by_node=device_by_node,
        pool=cfg.p2p_pool,
        topology_name=topo.name,
    )
    cd_by_link = _assign_collision_domains(links_sorted, topology_name=topo.name)

    # Build per-node interface lists in sorted link-id order.
    ifaces_by_device: dict[str, list[PlannedInterface]] = {
        device: [] for device in device_by_node.values()
    }
    planned_links: list[PlannedLink] = []

    for link in links_sorted:
        metric = link_metric(link, cfg)
        a_dev = device_by_node[link.source]
        b_dev = device_by_node[link.target]
        # Stable endpoint ordering by device name (not SNDlib source/target).
        if a_dev <= b_dev:
            left_dev, right_dev = a_dev, b_dev
        else:
            left_dev, right_dev = b_dev, a_dev

        left_addr, right_addr, subnet = link_addrs[link.id]
        cd = cd_by_link[link.id]
        left_iface_name = f"eth{len(ifaces_by_device[left_dev])}"
        right_iface_name = f"eth{len(ifaces_by_device[right_dev])}"
        prefixlen = subnet.prefixlen

        left_iface = PlannedInterface(
            name=left_iface_name,
            link_id=link.id,
            collision_domain=cd,
            peer_device=right_dev,
            peer_address=str(right_addr),
            address=str(left_addr),
            prefixlen=prefixlen,
            subnet=str(subnet),
            metric=metric,
        )
        right_iface = PlannedInterface(
            name=right_iface_name,
            link_id=link.id,
            collision_domain=cd,
            peer_device=left_dev,
            peer_address=str(left_addr),
            address=str(right_addr),
            prefixlen=prefixlen,
            subnet=str(subnet),
            metric=metric,
        )
        ifaces_by_device[left_dev].append(left_iface)
        ifaces_by_device[right_dev].append(right_iface)

        planned_links.append(
            PlannedLink(
                link_id=link.id,
                collision_domain=cd,
                source_node_id=link.source,
                target_node_id=link.target,
                endpoint_a=left_dev,
                endpoint_b=right_dev,
                iface_a=left_iface_name,
                iface_b=right_iface_name,
                address_a=str(left_addr),
                address_b=str(right_addr),
                subnet=str(subnet),
                metric=metric,
            )
        )

    planned_nodes: list[PlannedNode] = []
    for node in nodes_sorted:
        device = device_by_node[node.id]
        loopback = loopbacks[node.id]
        ifaces = tuple(ifaces_by_device[device])
        startup = _startup_commands(loopback=loopback, interfaces=ifaces)
        planned = PlannedNode(
            node_id=node.id,
            device_name=device,
            loopback=str(loopback),
            router_id=str(loopback),
            interfaces=ifaces,
            startup_commands=startup,
            frr_conf="",  # filled below
        )
        frr_conf = render_frr_conf(planned, igp=cfg.igp, interfaces=ifaces)
        planned_nodes.append(
            PlannedNode(
                node_id=planned.node_id,
                device_name=planned.device_name,
                loopback=planned.loopback,
                router_id=planned.router_id,
                interfaces=planned.interfaces,
                startup_commands=planned.startup_commands,
                frr_conf=frr_conf,
            )
        )

    nodes_t = tuple(planned_nodes)
    links_t = tuple(planned_links)
    inventory = build_inventory(
        topology_name=topo.name,
        igp=cfg.igp,
        metric_strategy=cfg.metric_strategy,
        constant_metric=cfg.constant_metric,
        nodes=nodes_t,
        links=links_t,
    )
    return IspPlan(
        topology_name=topo.name,
        igp=cfg.igp,
        metric_strategy=cfg.metric_strategy,
        constant_metric=cfg.constant_metric,
        nodes=nodes_t,
        links=links_t,
        inventory=inventory,
    )


def build_inventory(
    *,
    topology_name: str,
    igp: str,
    metric_strategy: str,
    constant_metric: int,
    nodes: tuple[PlannedNode, ...],
    links: tuple[PlannedLink, ...],
) -> dict[str, Any]:
    """Stable, JSON-serializable mapping from SNDlib entities to runtime resources."""
    node_rows = []
    for node in sorted(nodes, key=lambda n: n.node_id):
        iface_rows = [
            {
                "name": iface.name,
                "link_id": iface.link_id,
                "collision_domain": iface.collision_domain,
                "peer_device": iface.peer_device,
                "peer_address": iface.peer_address,
                "address": f"{iface.address}/{iface.prefixlen}",
                "subnet": iface.subnet,
                "metric": iface.metric,
                "passive": iface.passive,
            }
            for iface in sorted(node.interfaces, key=lambda i: i.name)
        ]
        node_rows.append(
            {
                "node_id": node.node_id,
                "device": node.device_name,
                "loopback": f"{node.loopback}/32",
                "router_id": node.router_id,
                "interfaces": iface_rows,
            }
        )
    link_rows = [
        {
            "link_id": link.link_id,
            "collision_domain": link.collision_domain,
            "source_node_id": link.source_node_id,
            "target_node_id": link.target_node_id,
            "endpoint_a": {
                "device": link.endpoint_a,
                "iface": link.iface_a,
                "address": f"{link.address_a}/31",
            },
            "endpoint_b": {
                "device": link.endpoint_b,
                "iface": link.iface_b,
                "address": f"{link.address_b}/31",
            },
            "subnet": link.subnet,
            "metric": link.metric,
        }
        for link in sorted(links, key=lambda item: item.link_id)
    ]
    return {
        "topology_name": topology_name,
        "igp": igp,
        "metric_strategy": metric_strategy,
        "constant_metric": constant_metric,
        "node_count": len(node_rows),
        "link_count": len(link_rows),
        "nodes": node_rows,
        "links": link_rows,
    }


def _assign_device_names(nodes: tuple, *, topology_name: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    used: dict[str, str] = {}
    for node in nodes:
        slug = slugify(node.id, kind="node")
        if slug in used:
            raise IspCompileError(
                f"Device name collision: {node.id!r} and {used[slug]!r} "
                f"both map to {slug!r}.",
                topology=topology_name,
            )
        used[slug] = node.id
        mapping[node.id] = slug
    return mapping


def _assign_collision_domains(
    links: tuple[TopoLink, ...], *, topology_name: str
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    used: dict[str, str] = {}
    for link in links:
        slug = slugify(link.id, kind="link")
        # Collision domains must be unique; prefix to avoid clashing with device names.
        cd = f"cd_{slug}"
        if cd in used:
            raise IspCompileError(
                f"Collision domain collision: {link.id!r} and {used[cd]!r} "
                f"both map to {cd!r}.",
                topology=topology_name,
            )
        used[cd] = link.id
        mapping[link.id] = cd
    return mapping


def _assign_loopbacks(
    nodes: tuple, *, pool: IPv4Network, topology_name: str
) -> dict[str, IPv4Address]:
    # Reserve .0 as network; assign sequential hosts from network+1.
    available = pool.num_addresses - 2  # exclude network + broadcast when applicable
    if pool.prefixlen == 32:
        available = 1
    elif pool.prefixlen == 31:
        available = 2
    if len(nodes) > available:
        raise IspCompileError(
            f"Loopback pool {pool} has at most {available} addresses; "
            f"need {len(nodes)}.",
            topology=topology_name,
        )
    mapping: dict[str, IPv4Address] = {}
    # Start at network_address + 1 for readability (10.255.0.1 ...).
    base = int(pool.network_address) + (0 if pool.prefixlen >= 31 else 1)
    for index, node in enumerate(nodes):
        addr = IPv4Address(base + index)
        if addr not in pool:
            raise IspCompileError(
                f"Loopback pool {pool} exhausted at node {node.id!r}.",
                topology=topology_name,
            )
        mapping[node.id] = addr
    return mapping


def _assign_link_addresses(
    links: tuple[TopoLink, ...],
    *,
    device_by_node: dict[str, str],
    pool: IPv4Network,
    topology_name: str,
) -> dict[str, tuple[IPv4Address, IPv4Address, IPv4Network]]:
    """Return link_id → (addr_left, addr_right, /31 subnet) with left by device name."""
    if pool.prefixlen > 31:
        raise IspCompileError(
            f"P2P pool {pool} is too small for /31 links.",
            topology=topology_name,
        )
    # Avoid pool.subnets(new_prefix=31) which materializes millions of nets for /8.
    total_slash31 = 1 << (31 - pool.prefixlen)
    if len(links) > total_slash31:
        raise IspCompileError(
            f"P2P pool {pool} provides {total_slash31} /31 subnets; need {len(links)}.",
            topology=topology_name,
        )
    mapping: dict[str, tuple[IPv4Address, IPv4Address, IPv4Network]] = {}
    base = int(pool.network_address)
    for index, link in enumerate(links):
        if link.source not in device_by_node or link.target not in device_by_node:
            raise IspCompileError(
                f"Link {link.id!r} references unknown nodes "
                f"{link.source!r} / {link.target!r}.",
                topology=topology_name,
            )
        net_addr = IPv4Address(base + index * 2)
        subnet = IPv4Network(f"{net_addr}/31")
        if (
            subnet.network_address not in pool
            or (subnet.network_address + 1) not in pool
        ):
            raise IspCompileError(
                f"P2P pool {pool} exhausted at link {link.id!r}.",
                topology=topology_name,
            )
        # address_a always binds to the lexicographically smaller device name
        # (resolved later in compile_isp_plan).
        mapping[link.id] = (subnet.network_address, subnet.network_address + 1, subnet)
    return mapping


def _startup_commands(
    *, loopback: IPv4Address, interfaces: tuple[PlannedInterface, ...]
) -> tuple[str, ...]:
    cmds = [f"ip addr add {loopback}/32 dev lo"]
    for iface in interfaces:
        cmds.append(f"ip addr add {iface.address}/{iface.prefixlen} dev {iface.name}")
    cmds.append("service frr start")
    return tuple(cmds)
