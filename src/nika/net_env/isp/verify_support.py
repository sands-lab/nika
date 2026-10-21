"""Shared ISP verification selection and contract probe declarations."""

from __future__ import annotations

from dataclasses import replace
from ipaddress import IPv4Network

from nika.net_env.contract import ValidationContract, ValidationIntent
from nika.net_env.isp.bgp.plan import BgpPlan
from nika.net_env.isp.igp.plan import igp_components
from nika.net_env.isp.traffic.stubs import IspTrafficAttachment
from nika.net_env.verify_collection import VerifyCommand


def sample_traffic(
    traffic: IspTrafficAttachment | None,
    contract: ValidationContract | None = None,
    bgp_plan: BgpPlan | None = None,
) -> IspTrafficAttachment | None:
    if traffic is None:
        return None
    selected = set()
    by_router = {host.router_device: host for host in traffic.hosts}
    for component in igp_components(traffic.plan):
        hosts = [by_router[router] for router in component if router in by_router]
        if hosts:
            selected.update((hosts[0].host_name, hosts[-1].host_name))
    if contract:
        selected.update(
            entity.name
            for intent in contract.intents
            for entity in (intent.source, intent.destination)
            if entity is not None and entity.kind == "endpoint"
        )
    if bgp_plan:
        selected.add(str(bgp_plan.inventory.get("data_plane_observer_host") or ""))
    return replace(
        traffic,
        hosts=tuple(host for host in traffic.hosts if host.host_name in selected),
        edge_links=tuple(
            edge for edge in traffic.edge_links if edge.host_name in selected
        ),
    )


def intent_command(intent: ValidationIntent) -> VerifyCommand:
    required = intent.level == "required"
    if intent.route_next_hops is not None:
        return VerifyCommand(
            intent.route_next_hops.node,
            f"vtysh -c 'show ip route {'' if intent.route_next_hops.vrf == 'default' else 'vrf ' + intent.route_next_hops.vrf + ' '}{intent.route_next_hops.prefix} json'",
            20,
            required=required,
        )
    if intent.bgp_route is not None:
        return VerifyCommand(
            intent.bgp_route.node,
            f"vtysh -c 'show bgp ipv4 unicast {intent.bgp_route.prefix} json'",
            20,
            required=required,
        )
    if intent.route_policy is not None:
        return VerifyCommand(
            intent.route_policy.node,
            f"vtysh -c 'show bgp ipv4 unicast neighbors {intent.route_policy.remote_address} advertised-routes json'",
            20,
            required=required,
        )
    if intent.property == "adjacency":
        assert intent.adjacency is not None
        command = (
            "vtysh -c 'show bgp summary'"
            if intent.adjacency.protocol == "bgp"
            else (
                "vtysh -c 'show isis neighbor'"
                if intent.adjacency.protocol == "isis"
                else "vtysh -c 'show ip ospf neighbor'"
            )
        )
        return VerifyCommand(intent.adjacency.local_node, command, required=required)
    assert intent.source is not None and intent.destination is not None
    source = (
        intent.source.name
        if intent.source.kind in {"node", "endpoint"}
        else intent.source.node
    )
    if source is None:
        raise ValueError(
            f"entity {intent.source.name!r} cannot be used as a runtime source"
        )
    address = intent.destination.address
    if not address:
        raise ValueError(f"entity {intent.destination.name!r} has no address")
    target = (
        str(IPv4Network(address).network_address + 1)
        if intent.destination.kind == "prefix"
        else address.split("/")[0]
    )
    if intent.property == "waypoint":
        source_address = f"-s {intent.source.address} " if intent.source.address else ""
        protocol = "-I " if intent.traffic and intent.traffic.protocol == "icmp" else ""
        return VerifyCommand(
            source,
            f"traceroute {protocol}{source_address}-n -m 32 -w 1 -q 1 {target}",
            40,
            required=required,
        )
    assert intent.traffic is not None
    if intent.traffic.protocol in {"tcp", "udp"}:
        flag = "-u " if intent.traffic.protocol == "udp" else ""
        return VerifyCommand(
            source,
            f"nc -z {flag}-w 2 {target} {intent.traffic.destination_port} && echo NIKA_OPEN",
            10,
            required=required,
        )
    source_address = f"-I {intent.source.address} " if intent.source.address else ""
    return VerifyCommand(
        source, f"ping {source_address}-c 1 -W 2 {target}", 15, required=required
    )


def intent_commands(contract: ValidationContract) -> list[VerifyCommand]:
    """Declare executable requests; unsupported entities get per-intent errors."""
    commands = []
    for intent in contract.intents:
        try:
            commands.append(intent_command(intent))
            if intent.route_policy is not None:
                commands.append(
                    VerifyCommand(
                        intent.route_policy.node,
                        f"vtysh -c 'show bgp ipv4 unicast {intent.route_policy.prefix} json'",
                        20,
                    )
                )
            if (
                intent.adjacency is not None
                and intent.adjacency.route_reflector_client is not None
            ):
                commands.append(
                    VerifyCommand(
                        intent.adjacency.local_node,
                        "vtysh -c 'show bgp neighbors json'",
                    )
                )
        except ValueError:
            # The evaluator reports this intent's declaration error while
            # retaining evidence from the remaining valid requests.
            continue
    return commands
