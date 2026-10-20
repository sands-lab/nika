"""Build direct-eBGP experiments from original vendor-neutral routing inputs."""

from ipaddress import IPv4Interface

from nika.net_env.contract import ValidationContract, ValidationIntent
from nika.net_env.net_env_pool import get_net_env_instance
from nika.workflows.validation.scenario_spec import (
    ScenarioExperiment,
    ScenarioIncident,
    RoutingMutation,
    RoutingRuntimeMutation,
)


def build_direct_bgp_experiment(scenario, *, routing_model):
    """Reuse the same session, host-flow, completion and outage logic per platform.

    The existing lab's original router declarations and host startup addressing
    supply requirements before any solver invocation or candidate rendering.
    """
    env = get_net_env_instance(scenario)
    if routing_model not in env.ROUTING_MODELS:
        raise ValueError("Routing model does not match scenario capabilities")
    routers = env.BGP_ROUTERS
    by_address = {str(r["link_ip"].ip): name for name, r in routers.items()}
    if len(by_address) != len(routers):
        raise ValueError("Direct BGP sketch requires distinct local addresses")
    intents = []
    for node, router in routers.items():
        remote = by_address[str(router["peer_ip"])]
        intents.append(
            ValidationIntent.model_validate(
                {
                    "id": f"adj.{node}.{remote}",
                    "description": "The original direct eBGP session establishes.",
                    "property": "adjacency",
                    "expected": "established",
                    "adjacency": {
                        "protocol": "bgp",
                        "local_node": node,
                        "remote_node": remote,
                        "local_address": str(router["link_ip"].ip),
                        "remote_address": str(router["peer_ip"]),
                        "local_asn": router["as"],
                        "remote_asn": router["peer_as"],
                        "session_type": "ebgp",
                    },
                }
            )
        )
    from nika.validation.batfish.kathara import startup_addresses

    hosts = {}
    for node in set(env.lab.machines) - set(routers):
        if env.lab.fs.exists(f"{node}.startup"):
            addresses = startup_addresses(env.lab.fs.readtext(f"{node}.startup"))
            if len(addresses) == 1:
                hosts[node] = str(IPv4Interface(addresses[0][0]).ip)
    if len(hosts) < 2:
        raise ValueError("Direct BGP experiment requires addressed Linux endpoints")
    for source, address in sorted(hosts.items()):
        for dest, remote in sorted(hosts.items()):
            if source != dest:
                intents.append(
                    ValidationIntent.model_validate(
                        {
                            "id": f"reach.{source}.{dest}",
                            "description": "Original client prefixes communicate over BGP.",
                            "property": "reachability",
                            "expected": "reachable",
                            "source": {
                                "kind": "node",
                                "name": source,
                                "address": address,
                            },
                            "destination": {
                                "kind": "node",
                                "name": dest,
                                "address": remote,
                            },
                            "traffic": {"protocol": "icmp"},
                        }
                    )
                )
    contract = ValidationContract(
        scenario=scenario,
        contract_id=f"routing.{scenario}.bgp",
        design_source={
            "basis": "RFC 4271 direct eBGP and original lab addressing/AS/prefix declarations"
        },
        intents=tuple(intents),
    )
    node = sorted(routers)[0]
    # Removing the sole transit edge of a two-router direct BGP component
    # disconnects its client prefixes.
    # Larger graphs need independently authored partition/redundancy requirements.
    if len(routers) != 2:
        raise ValueError("Automatic sole-transit outage requirements need two routers")
    interface = next(
        i
        for i, native in env.BGP_INTERFACE_MAP.items()
        if native == env.BGP_PEERING_INTERFACE
    )
    outage = tuple(
        i.model_copy(update={"property": "isolation", "expected": "unreachable"})
        for i in intents
        if i.property == "reachability"
    )
    original = routers[node]
    local_network = original["pc_ip"].network
    local_hosts = {
        h for h, address in hosts.items() if IPv4Interface(address).ip in local_network
    }
    if (
        len(hosts) != 2
        or len(local_hosts) != 1
        or any(
            sum(
                IPv4Interface(address).ip in router["pc_ip"].network
                for router in routers.values()
            )
            != 1
            for address in hosts.values()
        )
    ):
        raise ValueError(
            "Sole-transit outage requires one host in each original client prefix"
        )
    affected_static = tuple(
        i.id
        for i in intents
        if i.property == "reachability" and i.destination.name in local_hosts
    )
    export_old = (
        "  done"
        if routing_model == "iosxr-bgp"
        else f"if (dst == {local_network} && dst-len == {local_network.prefixlen}) {{ accept; }}"
    )
    export_new = (
        "  drop"
        if routing_model == "iosxr-bgp"
        else export_old.replace("accept;", "reject;")
    )
    all_ids = tuple(i.id for i in contract.intents)
    old_asn = (
        f"remote-as {original['peer_as']}"
        if routing_model == "iosxr-bgp"
        else f"remote.as={original['peer_as']}"
    )
    bad_asn = (
        f"remote-as {original['peer_as'] + 100}"
        if routing_model == "iosxr-bgp"
        else f"remote.as={original['peer_as'] + 100}"
    )
    native_controls = ()
    if routing_model == "routeros-bgp":
        connection = f"[find where remote.address={original['peer_ip']}]"
        native_controls = (
            RoutingRuntimeMutation(
                name="peer-disabled",
                node=node,
                commands=(f"/routing/bgp/connection disable {connection}",),
                recovery=(f"/routing/bgp/connection enable {connection}",),
                expected_failed_ids=all_ids,
            ),
        )
    return ScenarioExperiment(
        scenario=scenario,
        routing_model=routing_model,
        healthy=contract,
        completion={"kind": "bgp_fabric", "origins": env.bgp_origin_prefixes()},
        convergence_timeout_sec=600,
        counterexamples=(
            RoutingMutation(
                name="wrong-peer-asn",
                node=node,
                old=old_asn,
                new=bad_asn,
                expected_failed_ids=all_ids,
            ),
            RoutingMutation(
                name="missing-export",
                node=node,
                old=export_old,
                new=export_new,
                expected_failed_ids=affected_static,
            ),
        ),
        runtime_counterexamples=native_controls,
        incidents=(
            ScenarioIncident(
                name="transit-down",
                host_name=node,
                intf_name=interface,
                requirements=contract.model_copy(update={"intents": outage}),
            ),
        ),
    )
