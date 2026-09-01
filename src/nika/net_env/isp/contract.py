"""Healthy-baseline validation contract generation for ISP design plans."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from ipaddress import IPv4Network

from nika.net_env.contract import (
    AdjacencyExpectation,
    EntitySelector,
    NetworkEntity,
    PathConstraint,
    SelectorCatalog,
    TrafficSelector,
    ValidationContract,
    ValidationIntent,
)
from nika.net_env.isp.bgp.plan import BgpPlan
from nika.net_env.isp.igp.plan import IspPlan, active_igp_links
from nika.net_env.isp.traffic.stubs import IspTrafficAttachment


@dataclass(frozen=True)
class IspValidationPolicy:
    """Explicit scenario-design policy that is independent of generated config."""

    denied_external_prefixes: tuple[str, ...] = ("192.0.2.0/24",)


def build_isp_validation_contract(
    plan: IspPlan,
    *,
    traffic: IspTrafficAttachment,
    bgp_plan: BgpPlan | None = None,
    policy: IspValidationPolicy = IspValidationPolicy(),
    scenario: str | None = None,
) -> ValidationContract:
    """Compile stable concrete intents from ISP topology and routing design."""
    catalog = _catalog(plan, traffic, bgp_plan, policy)
    endpoints = catalog.expand(EntitySelector(kind="group", value="edge_endpoints"))
    baseline_endpoints = _baseline_endpoints(endpoints, bgp_plan)
    intents: list[ValidationIntent] = []
    scenario_id = scenario or f"isp_{plan.topology_name}"

    if len(baseline_endpoints) >= 2:
        source, destination = baseline_endpoints
        intents.append(
            ValidationIntent(
                id=f"reach.edge.{source.name}.{destination.name}.icmp",
                description=f"Edge endpoint {source.name} must reach {destination.name} over ICMP.",
                property="reachability",
                expected="reachable",
                source=source,
                destination=destination,
                traffic=TrafficSelector(protocol="icmp"),
            )
        )
        path = _path_constraint(plan, source.node, destination.node)
        if path is not None:
            intents.append(
                ValidationIntent(
                    id=f"path.edge.{source.name}.{destination.name}",
                    description="Edge traffic must follow the paths allowed by the IGP metric design.",
                    property="waypoint",
                    expected="path_compliant",
                    level="optional",
                    source=source,
                    destination=destination,
                    traffic=TrafficSelector(protocol="icmp"),
                    path=path,
                )
            )

    if endpoints:
        source = endpoints[0]
        for prefix in catalog.expand(
            EntitySelector(kind="group", value="denied_external_prefixes")
        ):
            intents.append(
                ValidationIntent(
                    id=f"isolate.edge.{source.name}.{prefix.name}.ipv4",
                    description=f"Edge endpoint {source.name} must not reach denied external prefix {prefix.address}.",
                    property="isolation",
                    expected="unreachable",
                    source=source,
                    destination=prefix,
                    traffic=TrafficSelector(protocol="ipv4"),
                )
            )

    if plan.igp == "ospf":
        router_id = {node.device_name: node.router_id for node in plan.nodes}
        active_links = {link.link_id for link in active_igp_links(plan)}
        for link in sorted(plan.links, key=lambda item: item.link_id):
            if link.link_id not in active_links:
                continue
            intents.append(
                ValidationIntent(
                    id=(
                        f"adj.ospf.{link.endpoint_a}.{link.endpoint_b}."
                        f"{link.address_a.replace('.', '-')}"
                    ),
                    description=f"OSPF adjacency {link.endpoint_a} to {link.endpoint_b} must be Full.",
                    property="adjacency",
                    expected="established",
                    adjacency=AdjacencyExpectation(
                        protocol="ospf",
                        local_node=link.endpoint_a,
                        remote_node=link.endpoint_b,
                        local_address=link.address_a,
                        remote_address=link.address_b,
                        ospf_area="0.0.0.0",
                        local_router_id=router_id[link.endpoint_a],
                        remote_router_id=router_id[link.endpoint_b],
                    ),
                )
            )

    if bgp_plan is not None:
        for session in sorted(
            bgp_plan.sessions,
            key=lambda item: (item.local_device, item.remote_device, item.remote_ip),
        ):
            intents.append(
                ValidationIntent(
                    id=f"adj.bgp.{session.local_device}.{session.remote_device}.{session.remote_ip.replace('.', '-')}",
                    description=f"BGP session {session.local_device} to {session.remote_device} must be established.",
                    property="adjacency",
                    expected="established",
                    adjacency=AdjacencyExpectation(
                        protocol="bgp",
                        local_node=session.local_device,
                        remote_node=session.remote_device,
                        local_address=session.local_ip,
                        remote_address=session.remote_ip,
                        local_asn=session.local_asn,
                        remote_asn=session.remote_asn,
                        update_source=session.update_source,
                        session_type=session.session_type,
                    ),
                )
            )
        prefix_by_value = {
            entity.address: entity
            for entity in catalog.entities
            if entity.kind == "prefix"
        }
        node_by_name = {
            entity.name: entity for entity in catalog.entities if entity.kind == "node"
        }
        for observer, prefix in sorted(bgp_plan.expect_reachable):
            destination = prefix_by_value[prefix]
            intents.append(
                ValidationIntent(
                    id=f"reach.bgp.{observer}.{destination.name}.icmp",
                    description=f"BGP observer {observer} must reach designed prefix {prefix}.",
                    property="reachability",
                    expected="reachable",
                    source=node_by_name[observer],
                    destination=destination,
                    traffic=TrafficSelector(protocol="icmp"),
                )
            )

    return ValidationContract(
        contract_id=(
            f"isp.{plan.topology_name}.{plan.igp}.{plan.metric_strategy}."
            f"{plan.constant_metric}.{bgp_plan.mode if bgp_plan else 'none'}"
        ),
        scenario=scenario_id,
        design_source={
            "topology": plan.topology_name,
            "igp": plan.igp,
            "metric_strategy": plan.metric_strategy,
            "constant_metric": plan.constant_metric,
            "bgp_mode": bgp_plan.mode if bgp_plan else "none",
            "rpki": bool(bgp_plan.inventory.get("rpki")) if bgp_plan else False,
            "denied_external_prefixes": list(policy.denied_external_prefixes),
        },
        intents=tuple(sorted(intents, key=lambda intent: intent.id)),
    )


def _catalog(
    plan: IspPlan,
    traffic: IspTrafficAttachment,
    bgp_plan: BgpPlan | None,
    policy: IspValidationPolicy,
) -> SelectorCatalog:
    entities: list[NetworkEntity] = [
        NetworkEntity(kind="node", name=node.device_name, address=node.loopback)
        for node in plan.nodes
    ]
    endpoint_names: list[str] = []
    for host in traffic.hosts:
        endpoint_names.append(host.host_name)
        entities.append(
            NetworkEntity(
                kind="endpoint",
                name=host.host_name,
                address=host.address,
                node=host.router_device,
            )
        )
    denied_names: list[str] = []
    for prefix in sorted(policy.denied_external_prefixes):
        network = IPv4Network(prefix)
        name = f"denied_{str(network.network_address).replace('.', '_')}_{network.prefixlen}"
        denied_names.append(name)
        entities.append(
            NetworkEntity(
                kind="prefix",
                name=name,
                address=str(network),
            )
        )
    if bgp_plan is not None:
        for originated in sorted(bgp_plan.originated, key=lambda item: item.prefix):
            network = IPv4Network(originated.prefix)
            entities.append(
                NetworkEntity(
                    kind="prefix",
                    name=f"bgp_{str(network.network_address).replace('.', '_')}_{network.prefixlen}",
                    address=str(network),
                    node=originated.device,
                )
            )
    return SelectorCatalog(
        entities=tuple(sorted(entities, key=lambda entity: entity.name)),
        groups={
            "edge_endpoints": tuple(sorted(endpoint_names)),
            "denied_external_prefixes": tuple(sorted(denied_names)),
            "routers": tuple(sorted(node.device_name for node in plan.nodes)),
        },
    )


def _path_constraint(
    plan: IspPlan, source_node: str | None, destination_node: str | None
) -> PathConstraint | None:
    if (
        source_node is None
        or destination_node is None
        or source_node == destination_node
    ):
        return None
    graph: dict[str, list[tuple[str, int]]] = {
        node.device_name: [] for node in plan.nodes
    }
    active_links = {link.link_id for link in active_igp_links(plan)}
    for link in plan.links:
        if link.link_id not in active_links:
            continue
        graph[link.endpoint_a].append((link.endpoint_b, link.metric))
        graph[link.endpoint_b].append((link.endpoint_a, link.metric))
    from_source = _shortest_distances(graph, source_node)
    from_destination = _shortest_distances(graph, destination_node)
    total = from_source.get(destination_node)
    if total is None:
        return None
    on_shortest = {
        node
        for node in graph
        if from_source.get(node, total + 1) + from_destination.get(node, total + 1)
        == total
    }
    avoid = tuple(sorted(set(graph) - on_shortest)[:1])
    if not avoid:
        return None
    return PathConstraint(must_avoid=avoid)


def _baseline_endpoints(
    endpoints: tuple[NetworkEntity, ...], bgp_plan: BgpPlan | None
) -> tuple[NetworkEntity, ...]:
    if bgp_plan is None or bgp_plan.mode != "ebgp":
        return endpoints[:1] + endpoints[-1:] if len(endpoints) >= 2 else endpoints
    asn_of = {node.device_name: node.asn for node in bgp_plan.nodes}
    by_asn: dict[int, list[NetworkEntity]] = {}
    for endpoint in endpoints:
        if endpoint.node in asn_of:
            by_asn.setdefault(asn_of[endpoint.node], []).append(endpoint)
    for asn in sorted(by_asn):
        group = sorted(by_asn[asn], key=lambda item: item.name)
        if len(group) >= 2:
            return (group[0], group[-1])
    return ()


def _shortest_distances(
    graph: dict[str, list[tuple[str, int]]], source: str
) -> dict[str, int]:
    distances = {source: 0}
    queue = [(0, source)]
    while queue:
        distance, node = heapq.heappop(queue)
        if distance != distances[node]:
            continue
        for peer, metric in graph[node]:
            candidate = distance + metric
            if candidate < distances.get(peer, candidate + 1):
                distances[peer] = candidate
                heapq.heappush(queue, (candidate, peer))
    return distances
