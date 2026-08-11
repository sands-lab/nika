"""Compile stub hosts and edge /30 LANs onto an ISP plan."""

from __future__ import annotations

from dataclasses import dataclass, replace
from ipaddress import IPv4Address, IPv4Network

from nika.net_env.isp.igp.frr import render_frr_conf
from nika.net_env.isp.igp.ifaces import srl_e1_name
from nika.net_env.isp.igp.plan import (
    IspPlan,
    PlannedInterface,
    PlannedNode,
    build_inventory,
    slugify,
)
from nika.net_env.isp.traffic.models import (
    DEFAULT_EDGE_POOL_CIDR,
    TrafficMatrixSeries,
    stub_host_name,
)


@dataclass(frozen=True)
class PlannedHost:
    """Stub host attached to one router PoP for traffic injection."""

    host_name: str
    router_device: str
    router_node_id: str
    collision_domain: str
    address: str
    gateway: str
    subnet: str
    prefixlen: int
    startup_commands: tuple[str, ...]
    host_iface: str = "eth0"


@dataclass(frozen=True)
class PlannedEdgeLink:
    router_device: str
    host_name: str
    collision_domain: str
    router_iface: str
    router_address: str
    host_address: str
    subnet: str
    prefixlen: int


@dataclass(frozen=True)
class IspTrafficAttachment:
    series: TrafficMatrixSeries
    scale: float
    hosts: tuple[PlannedHost, ...]
    edge_links: tuple[PlannedEdgeLink, ...]
    plan: IspPlan


def attach_traffic_stubs(
    plan: IspPlan,
    series: TrafficMatrixSeries,
    *,
    scale: float = 1.0,
    edge_pool: IPv4Network | None = None,
    pop_node_ids: tuple[str, ...] | None = None,
    host_iface: str = "eth0",
    render_frr: bool = True,
) -> IspTrafficAttachment:
    """Add passive edge /30 stubs for active PoPs.

    When ``render_frr`` is True (Kathara default), node ``frr_conf`` / startup
    commands are re-rendered. Containerlab / SRL binders should pass
    ``render_frr=False`` and generate NOS config themselves.
    """
    pool = edge_pool or IPv4Network(DEFAULT_EDGE_POOL_CIDR)
    device_by_node = {n.node_id: n.device_name for n in plan.nodes}
    active = pop_node_ids if pop_node_ids is not None else series.active_node_ids()
    missing = [nid for nid in active if nid not in device_by_node]
    if missing:
        raise ValueError(
            f"Traffic series references unknown SNDlib nodes: {missing[:5]}"
        )

    # Stable PoP order by device name.
    pops = sorted(active, key=lambda nid: device_by_node[nid])
    total_slash30 = 1 << (30 - pool.prefixlen) if pool.prefixlen <= 30 else 0
    if len(pops) > total_slash30:
        raise ValueError(
            f"Edge pool {pool} provides {total_slash30} /30s; need {len(pops)}."
        )

    edge_ifaces_by_device: dict[str, list[PlannedInterface]] = {
        n.device_name: [] for n in plan.nodes
    }
    hosts: list[PlannedHost] = []
    edge_links: list[PlannedEdgeLink] = []
    base = int(pool.network_address)

    for index, node_id in enumerate(pops):
        device = device_by_node[node_id]
        node = next(n for n in plan.nodes if n.device_name == device)
        net_addr = IPv4Address(base + index * 4)
        subnet = IPv4Network(f"{net_addr}/30")
        router_ip = subnet.network_address + 1
        host_ip = subnet.network_address + 2
        iface_name = f"eth{len(node.interfaces) + len(edge_ifaces_by_device[device])}"
        cd = f"cd_edge_{slugify(device, kind='node')}"
        host_name = stub_host_name(device)

        edge_iface = PlannedInterface(
            name=iface_name,
            link_id=f"edge_{device}",
            collision_domain=cd,
            peer_device=host_name,
            peer_address=str(host_ip),
            address=str(router_ip),
            prefixlen=30,
            subnet=str(subnet),
            metric=1,
            passive=True,
        )
        edge_ifaces_by_device[device].append(edge_iface)
        edge_links.append(
            PlannedEdgeLink(
                router_device=device,
                host_name=host_name,
                collision_domain=cd,
                router_iface=iface_name,
                router_address=str(router_ip),
                host_address=str(host_ip),
                subnet=str(subnet),
                prefixlen=30,
            )
        )
        hosts.append(
            PlannedHost(
                host_name=host_name,
                router_device=device,
                router_node_id=node_id,
                collision_domain=cd,
                address=str(host_ip),
                gateway=str(router_ip),
                subnet=str(subnet),
                prefixlen=30,
                host_iface=host_iface,
                startup_commands=(
                    f"ip addr add {host_ip}/30 dev {host_iface}",
                    f"ip route replace default via {router_ip}",
                ),
            )
        )

    new_nodes: list[PlannedNode] = []
    for node in plan.nodes:
        extra = tuple(edge_ifaces_by_device.get(node.device_name, ()))
        ifaces = node.interfaces + extra
        if render_frr:
            startup = _startup_with_ifaces(loopback=node.loopback, interfaces=ifaces)
            draft = PlannedNode(
                node_id=node.node_id,
                device_name=node.device_name,
                loopback=node.loopback,
                router_id=node.router_id,
                interfaces=ifaces,
                startup_commands=startup,
                frr_conf="",
            )
            frr = render_frr_conf(draft, igp=plan.igp, interfaces=ifaces)
            new_nodes.append(replace(draft, frr_conf=frr))
        else:
            new_nodes.append(
                PlannedNode(
                    node_id=node.node_id,
                    device_name=node.device_name,
                    loopback=node.loopback,
                    router_id=node.router_id,
                    interfaces=ifaces,
                    startup_commands=(),
                    frr_conf="",
                )
            )

    nodes_t = tuple(new_nodes)
    inventory = build_inventory(
        topology_name=plan.topology_name,
        igp=plan.igp,
        metric_strategy=plan.metric_strategy,
        constant_metric=plan.constant_metric,
        nodes=nodes_t,
        links=plan.links,
    )
    hosts_t = tuple(sorted(hosts, key=lambda h: h.host_name))
    edges_t = tuple(sorted(edge_links, key=lambda e: e.router_device))
    inventory["hosts"] = [
        {
            "host": h.host_name,
            "router_device": h.router_device,
            "router_node_id": h.router_node_id,
            "collision_domain": h.collision_domain,
            "address": f"{h.address}/{h.prefixlen}",
            "gateway": h.gateway,
            "subnet": h.subnet,
            "host_iface": h.host_iface,
        }
        for h in hosts_t
    ]
    inventory["edge_links"] = [
        {
            "router_device": e.router_device,
            "host_name": e.host_name,
            "collision_domain": e.collision_domain,
            "router_iface": e.router_iface,
            "router_address": f"{e.router_address}/{e.prefixlen}",
            "host_address": f"{e.host_address}/{e.prefixlen}",
            "subnet": e.subnet,
        }
        for e in edges_t
    ]
    inventory["traffic"] = {
        "stubs": True,
        "host_count": len(hosts_t),
        "scale_at_compile": scale,
        "layout_source": series.source,
        "layout_topology": series.topology,
    }

    new_plan = IspPlan(
        topology_name=plan.topology_name,
        igp=plan.igp,
        metric_strategy=plan.metric_strategy,
        constant_metric=plan.constant_metric,
        nodes=nodes_t,
        links=plan.links,
        inventory=inventory,
    )
    return IspTrafficAttachment(
        series=series,
        scale=scale,
        hosts=hosts_t,
        edge_links=edges_t,
        plan=new_plan,
    )


def remap_inventory_ifaces_to_srl(
    attachment: IspTrafficAttachment,
) -> IspTrafficAttachment:
    """Rewrite inventory/edge router iface names to Containerlab ``e1-N``.

    Plan interface names stay as ``ethN`` so SRL YAML renderers can map them.
    """

    def map_iface(name: str) -> str:
        return srl_e1_name(name)

    edges_t = tuple(
        replace(edge, router_iface=map_iface(edge.router_iface))
        for edge in attachment.edge_links
    )
    inventory = dict(attachment.plan.inventory)
    # Remap node interface names in inventory rows.
    remapped_nodes = []
    for node in inventory.get("nodes") or []:
        row = dict(node)
        ifaces = []
        for iface in row.get("interfaces") or []:
            iface_row = dict(iface)
            if iface_row.get("name"):
                iface_row["name"] = map_iface(str(iface_row["name"]))
            ifaces.append(iface_row)
        row["interfaces"] = ifaces
        remapped_nodes.append(row)
    inventory["nodes"] = remapped_nodes

    remapped_links = []
    for link in inventory.get("links") or []:
        row = dict(link)
        for ep_key in ("endpoint_a", "endpoint_b"):
            ep = dict(row.get(ep_key) or {})
            if ep.get("iface"):
                ep["iface"] = map_iface(str(ep["iface"]))
            row[ep_key] = ep
        remapped_links.append(row)
    inventory["links"] = remapped_links

    inventory["edge_links"] = [
        {
            "router_device": e.router_device,
            "host_name": e.host_name,
            "collision_domain": e.collision_domain,
            "router_iface": e.router_iface,
            "router_address": f"{e.router_address}/{e.prefixlen}",
            "host_address": f"{e.host_address}/{e.prefixlen}",
            "subnet": e.subnet,
        }
        for e in edges_t
    ]

    new_plan = IspPlan(
        topology_name=attachment.plan.topology_name,
        igp=attachment.plan.igp,
        metric_strategy=attachment.plan.metric_strategy,
        constant_metric=attachment.plan.constant_metric,
        nodes=attachment.plan.nodes,
        links=attachment.plan.links,
        inventory=inventory,
    )
    return IspTrafficAttachment(
        series=attachment.series,
        scale=attachment.scale,
        hosts=attachment.hosts,
        edge_links=edges_t,
        plan=new_plan,
    )


def _startup_with_ifaces(
    *, loopback: str, interfaces: tuple[PlannedInterface, ...]
) -> tuple[str, ...]:
    cmds = [f"ip addr add {loopback}/32 dev lo"]
    for iface in interfaces:
        cmds.append(f"ip addr add {iface.address}/{iface.prefixlen} dev {iface.name}")
    cmds.append("service frr start")
    return tuple(cmds)
