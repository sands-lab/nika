"""Original packet requirements must survive frontend and verifier changes."""

import pytest

from nika.net_env.contract import (
    NetworkEntity,
    PathConstraint,
    TrafficSelector,
    ValidationContract,
    ValidationIntent,
)
from nika.validation.netkat.model import (
    ForwardingAction,
    ForwardingEndpoint,
    ForwardingNetwork,
    ForwardingRule,
)
from nika.validation.netkat.verifier import NetKatVerifier, write_snapshot
from nika.workflows.validation.forwarding import build_forwarding_experiment


@pytest.mark.contract
@pytest.mark.parametrize("scenario", ["sdn_l3_clos", "p4_dc_fabric", "p4_dc_gateway"])
@pytest.mark.parametrize("size", ["s", "m", "l"])
def test_original_fabric_requirements_cover_attachments(scenario, size):
    spec = build_forwarding_experiment(scenario, topo_size=size)
    reachable = [i for i in spec.healthy.intents if i.property == "reachability"]
    clients = set(spec.healthy.design_source["client_endpoints"])
    services = set(spec.healthy.design_source["service_endpoints"])
    assert len(reachable) == 2 * len(clients) * len(services)
    assert {(i.source.name, i.destination.name) for i in reachable} == {
        (s, d) for s in clients for d in services
    } | {(s, d) for s in services for d in clients}
    assert all(i.traffic.round_trip for i in reachable)
    target = spec.incidents[0].host_name
    down = {i.id: i for i in spec.incidents[0].requirements.intents}
    for intent in reachable:
        affected = target in {intent.source.name, intent.destination.name}
        assert down[intent.id].expected == ("unreachable" if affected else "reachable")
    assert {i.id for i in spec.healthy.intents} == set(down)


@pytest.mark.e2e
def test_katch_priority_round_trip_and_path_properties(tmp_path, katch_image):
    endpoints = {
        "a": ForwardingEndpoint(
            address="10.0.0.1", switch="s", mac="02:00:00:00:00:01"
        ),
        "b": ForwardingEndpoint(
            address="10.0.0.2", switch="s", mac="02:00:00:00:00:02"
        ),
    }
    rules = tuple(
        ForwardingRule(
            prefix=f"{e.address}/32",
            priority=32,
            actions=(ForwardingAction(peer=name, destination_mac=e.mac),),
        )
        for name, e in endpoints.items()
    )
    network = ForwardingNetwork(endpoints=endpoints, rules={"s": rules})
    source = NetworkEntity(kind="endpoint", name="a", address="10.0.0.1")
    destination = NetworkEntity(kind="endpoint", name="b", address="10.0.0.2")
    reach = ValidationIntent(
        id="reach.a.b",
        description="Endpoint delivery",
        property="reachability",
        expected="reachable",
        source=source,
        destination=destination,
        traffic=TrafficSelector(protocol="icmp", round_trip=True),
    )
    waypoint = ValidationIntent(
        id="path.a.b",
        description="Delivery through s",
        property="waypoint",
        expected="path_compliant",
        source=source,
        destination=destination,
        traffic=TrafficSelector(protocol="icmp"),
        path=PathConstraint(must_traverse=("s",)),
    )
    contract = ValidationContract(
        contract_id="forwarding-regression",
        scenario="fabric",
        design_source={"policy": "a and b communicate through s"},
        intents=(waypoint, reach),
    )
    verifier = NetKatVerifier(image=katch_image)
    assert (
        verifier.verify(contract, write_snapshot(network, tmp_path / "healthy")).status
        == "passed"
    )
    # A broad higher-priority drop shadows a more specific destination prefix.
    denied = network.model_copy(
        update={
            "rules": {
                "s": (
                    ForwardingRule(prefix="0.0.0.0/0", priority=100, actions=()),
                    *rules,
                )
            }
        }
    )
    report = verifier.verify(contract, write_snapshot(denied, tmp_path / "denied"))
    assert {r.intent for r in report.results if r.status == "failed"} == {
        "path.a.b",
        "reach.a.b",
    }
    # One-way forwarding cannot satisfy the original round-trip requirement.
    one_way = network.model_copy(update={"rules": {"s": rules[1:]}})
    report = verifier.verify(contract, write_snapshot(one_way, tmp_path / "one-way"))
    assert next(r for r in report.results if r.intent == "reach.a.b").status == "failed"
    unsupported = contract.model_copy(
        update={
            "intents": (
                reach.model_copy(
                    update={
                        "traffic": TrafficSelector(protocol="tcp", destination_port=80)
                    }
                ),
            )
        }
    )
    report = verifier.verify(
        unsupported, write_snapshot(network, tmp_path / "unsupported")
    )
    assert report.status == "unsupported"
    assert report.results[0].supported is False


@pytest.mark.integration
@pytest.mark.parametrize("scenario", ["sdn_l3_clos", "p4_dc_fabric"])
def test_large_forwarding_counterexample_is_fully_classified(
    scenario, tmp_path, katch_image
):
    from nika.workflows.validation.scenario import validate_scenario_design

    spec = build_forwarding_experiment(scenario, topo_size="l")
    result = validate_scenario_design(spec, tmp_path)
    assert result["status"] == "passed"
    assert result["validation_scope"] == "static-only"
    assert result["static"]["healthy"]["passed"] == len(spec.healthy.intents)
    negative = result["static"]["missing-endpoint-route"]
    assert negative["errors"] == negative["unsupported"] == 0
    assert negative["passed"] + negative["failed"] == len(spec.healthy.intents)
    assert negative["failed"] == len(
        result["counterexamples"]["missing-endpoint-route"]["failed_ids"]
    )


@pytest.mark.contract
def test_openflow_projection_rejects_unknown_ipv4_actions():
    from nika.net_env.sdn_l3_clos.fabric_manager.forwarding_rules import (
        build_forwarding_rules,
    )
    from nika.net_env.sdn_l3_clos.topology_model import build_clos_fabric_model
    from nika.validation.netkat.adapters import openflow_network

    model = build_clos_fabric_model("s")
    candidate = build_forwarding_rules(model)
    flow = next(f for f in candidate["flows"] if f["match"].startswith("ip,"))
    flow["actions"] = "resubmit(,1)"
    with pytest.raises(ValueError, match="Unsupported IPv4 OpenFlow action"):
        openflow_network(model, candidate)


@pytest.mark.e2e
@pytest.mark.parametrize("scenario", ["sdn_l3_clos", "p4_dc_fabric", "p4_dc_gateway"])
def test_forwarding_candidate_native_fault_and_recovery(
    scenario, tmp_path, katch_image
):
    from nika.workflows.validation.scenario import validate_scenario_design

    result = validate_scenario_design(
        build_forwarding_experiment(scenario), tmp_path, runtime=True
    )
    assert result["status"] == "passed"
    assert result["validation_scope"] == "static-and-runtime"
    assert (
        result["counterexamples"]["missing-endpoint-route"]["runtime_status"]
        == "rejected"
    )
    assert (
        result["runtime"]["healthy"]
        == result["runtime"]["endpoint-link-down-recovered"]
    )
