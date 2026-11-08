"""Resolve shared Clos requirements into the routing experiment's original inputs."""

from nika.net_env.clos.design import build_clos_fabric_model, rack_prefix
from nika.net_env.clos.requirements import communication_contract
from nika.net_env.contract import ValidationIntent
from nika.net_env.dc_clos.design import router_asns, routing_links
from nika.workflows.validation.scenario_spec import ScenarioExperiment


def build_routing_clos_experiment(topo_size="s", *, device_profile="frr"):
    model = build_clos_fabric_model(topo_size)
    if device_profile not in {"frr", "iosxr", "routeros"}:
        raise ValueError(f"Unsupported Clos router profile: {device_profile}")
    if device_profile != "frr" and topo_size != "s":
        raise ValueError("Native Clos router profiles support size s only")
    asns = router_asns(model)
    links = routing_links(model)
    healthy = communication_contract(model, scenario="dc_clos", topo_size=topo_size)
    adjacencies = []
    for link in links:
        for local, remote, address, peer in (
            (link.leaf, link.spine, link.leaf_address, link.spine_address),
            (link.spine, link.leaf, link.spine_address, link.leaf_address),
        ):
            adjacencies.append(
                ValidationIntent.model_validate(
                    {
                        "id": f"adj.{local}.{remote}",
                        "description": "The declared fabric eBGP session must establish.",
                        "property": "adjacency",
                        "expected": "established",
                        "adjacency": {
                            "protocol": "bgp",
                            "local_node": local,
                            "remote_node": remote,
                            "local_address": address,
                            "remote_address": peer,
                            "local_asn": asns[local],
                            "remote_asn": asns[remote],
                            "session_type": "ebgp",
                        },
                    }
                )
            )
    healthy = healthy.model_copy(
        update={
            "intents": tuple(
                sorted(healthy.intents + tuple(adjacencies), key=lambda i: i.id)
            )
        }
    )
    failed_link = links[0]
    adjacency_ids = (
        f"adj.{failed_link.leaf}.{failed_link.spine}",
        f"adj.{failed_link.spine}.{failed_link.leaf}",
    )
    incident_contract = healthy.model_copy(
        update={
            "contract_id": "fabric-uplink-down",
            "intents": tuple(i for i in healthy.intents if i.id not in adjacency_ids),
        }
    )
    ecmp = []
    for leaf, destination in ((model.leaves[0], 2), (model.leaves[1], 1)):
        ecmp.append(
            ValidationIntent.model_validate(
                {
                    "id": f"ecmp.{leaf}",
                    "description": "All declared equal-cost next hops must be installed.",
                    "property": "route_next_hops",
                    "expected": "next_hops_match",
                    "route_next_hops": {
                        "node": leaf,
                        "prefix": rack_prefix(destination),
                        "next_hops": [
                            link.spine_address for link in links if link.leaf == leaf
                        ],
                    },
                }
            )
        )
    runtime = healthy.model_copy(
        update={"contract_id": "fabric-installed-ecmp", "intents": tuple(ecmp)}
    )
    available = set(model.leaf_spine_links) - {(failed_link.leaf, failed_link.spine)}
    incident_ecmp = []
    for intent in ecmp:
        source = intent.route_next_hops.node
        prefix = intent.route_next_hops.prefix
        destination = next(
            leaf for i, leaf in enumerate(model.leaves, 1) if rack_prefix(i) == prefix
        )
        hops = tuple(
            link.spine_address
            for link in links
            if link.leaf == source
            and (source, link.spine) in available
            and (destination, link.spine) in available
        )
        incident_ecmp.append(
            intent.model_copy(
                update={
                    "route_next_hops": intent.route_next_hops.model_copy(
                        update={"next_hops": hops}
                    ),
                }
            )
        )
    incident_runtime = runtime.model_copy(update={"intents": tuple(incident_ecmp)})
    node, peer, remote_asn = (
        failed_link.leaf,
        failed_link.spine_address,
        asns[failed_link.spine],
    )
    old = f"neighbor {peer} remote-as {remote_asn}"
    new = f"neighbor {peer} remote-as 65099"
    if device_profile == "routeros":
        old = f"remote.address={peer} remote.as={remote_asn}"
        new = f"remote.address={peer} remote.as=65099"
    # XR converts the spines; the bridged leaves retain their FRR configuration.
    target_leaf = model.leaves[1]
    affected_endpoints = {e.name for e in model.endpoints_on_leaf(2)}
    affected = [
        i.id
        for i in healthy.intents
        if i.property == "reachability"
        and (
            (i.source.name in affected_endpoints)
            != (i.destination.name in affected_endpoints)
        )
    ]
    failure_ids = affected + (
        [] if device_profile == "routeros" else [f"ecmp.{model.leaves[0]}"]
    )
    spec = {
        "scenario": "dc_clos",
        "topo_size": topo_size,
        "deployment": {}
        if device_profile == "frr"
        else {"device_profile": device_profile},
        "healthy": healthy,
        "completion": {
            "kind": "bgp_fabric",
            "origins": {rack_prefix(i): leaf for i, leaf in enumerate(model.leaves, 1)},
        },
        "incidents": [
            {
                "name": "uplink-down",
                "host_name": node,
                "intf_name": model.port_to_peer(node, failed_link.spine).name,
                "requirements": incident_contract,
                **(
                    {"runtime_requirements": incident_runtime}
                    if device_profile != "routeros"
                    else {}
                ),
            }
        ],
        "counterexamples": [
            {
                "name": "wrong-bgp-asn",
                "node": node,
                "old": old,
                "new": new,
                "expected_failed_ids": adjacency_ids,
            }
        ],
        "failure_checks": [
            {
                "name": "origin-withdrawal",
                "problem": "bgp_missing_route_advertisement",
                "params": {"host_name": target_leaf, "prefix": rack_prefix(2)},
                "restore_nodes": [target_leaf],
                "expected_failed_ids": failure_ids,
            }
        ],
    }
    if device_profile != "routeros":
        spec["runtime_requirements"] = runtime
        commands = [
            "configure terminal",
            f"router bgp {asns[node]}",
            "address-family ipv4 unicast",
        ]
        spec["runtime_counterexamples"] = [
            {
                "name": "missing-ecmp-path",
                "node": node,
                "commands": commands + ["maximum-paths 1", "end"],
                "recovery": commands + ["maximum-paths 32", "end"],
                "expected_failed_ids": [f"ecmp.{node}"],
            }
        ]
    return ScenarioExperiment.model_validate(spec)
