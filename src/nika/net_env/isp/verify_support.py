"""Shared ISP verification selection and contract probe declarations."""

from __future__ import annotations

from dataclasses import replace
from ipaddress import IPv4Network

from nika.net_env.contract import ValidationContract, ValidationIntent
from nika.net_env.isp.bgp.plan import BgpPlan
from nika.net_env.isp.igp.plan import igp_components
from nika.net_env.isp.traffic.stubs import IspTrafficAttachment
from nika.net_env.verify_collection import VerifyCommand, ping_command


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
    if intent.property == "adjacency":
        assert intent.adjacency is not None
        command = (
            "vtysh -c 'show bgp summary'"
            if intent.adjacency.protocol == "bgp"
            else "vtysh -c 'show ip ospf neighbor'"
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
        return VerifyCommand(
            source, f"traceroute -n -m 32 -w 1 -q 1 {target}", 40, required=required
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
    return VerifyCommand(source, ping_command(target), 15, required=required)


def intent_commands(contract: ValidationContract) -> list[VerifyCommand]:
    """Declare executable requests; unsupported entities get per-intent errors."""
    commands = []
    for intent in contract.intents:
        try:
            commands.append(intent_command(intent))
        except ValueError:
            # The evaluator reports this intent's declaration error while
            # retaining evidence from the remaining valid requests.
            continue
    return commands
