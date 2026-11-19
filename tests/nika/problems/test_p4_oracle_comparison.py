"""P4 Oracles require live capacity and the controller's current intent."""

import json
import shlex
from types import SimpleNamespace
import copy
import pytest
from nika.problems.forwarding_encapsulation_policy.p4_runtime import (
    P4RuntimePartialWrite,
    P4RuntimePartialWriteParams,
    P4TableResourceExhaustion,
    P4TableResourceExhaustionParams,
    P4EcmpGroupMemberMissing,
    P4EcmpGroupMemberMissingParams,
    P4ActionSelectorMemberMisconfig,
    P4ActionSelectorMemberMisconfigParams,
    declared_intent,
    _ecmp_target,
    intent_drift,
    _BAD_PORT,
)
from nika.net_env.p4_dc_gateway.topology_model import build_gateway_fabric_model
from nika.problems.link_interface.p4_gateway import (
    SilentEgressPacketLoss,
    SilentEgressPacketLossParams,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("size", ["s", "m", "l"])
@pytest.mark.parametrize("role", ["gateway", "spine"])
def test_loss_oracle_probes_egress_destinations_with_a_complete_time_budget(size, role):
    model = build_gateway_fabric_model(size)
    switch = f"{role}_1"
    peer = "spine_2" if role == "gateway" else "leaf_2"
    port = model.port_to_peer(switch, peer)
    problem = SilentEgressPacketLoss.__new__(SilentEgressPacketLoss)
    problem.net_env = SimpleNamespace(model=model)
    book = problem.oracle_playbook(
        SilentEgressPacketLossParams(
            host_name=switch, intf_name=port.name, bmv2_port=port.bmv2_port
        )
    )
    calls = [
        step.args
        for step in book.steps
        if step.purpose == "Send paced UDP probes across the affected egress."
    ]
    expected = model.services_on_leaf("leaf_2") if role == "spine" else model.services
    sources = model.clients if role == "spine" else [model.client_on_gateway(switch)]
    observed = []
    for args in calls:
        destinations = shlex.split(args["command"])[3:]
        assert args["timeout"] > len(destinations) * 12 * 32 * 0.02
        observed.extend((args["host_name"], ip) for ip in destinations)
    assert sorted(observed) == sorted(
        (source.name, service.ip) for source in sources for service in expected
    )
    predicate = book.steps[-1].expect
    assert predicate('{"observed":200,"missing":4}')
    assert not predicate('{"observed":200,"missing":0}')
    assert not predicate('{"observed":200,"missing":50}')


def comparison(routes, mismatches, *, pipeline=True):
    return json.dumps(
        {
            "summary": {
                "leaf_1": {
                    "pipeline_ok": pipeline,
                    "pipeline_matches_intent": pipeline,
                    "ipv4_lpm": routes,
                }
            },
            "mismatches": {"leaf_1": mismatches},
        }
    )


@pytest.mark.parametrize(
    "routes,mismatches,pipeline,expected",
    [
        (256, ["ipv4_lpm"], True, True),
        (255, ["ipv4_lpm"], True, False),
        (256, [], True, False),
        (256, ["groups"], True, False),
        (256, ["ipv4_lpm"], False, False),
    ],
)
def test_capacity_requires_full_table_and_live_intent_drift(
    routes, mismatches, pipeline, expected
):
    p = P4TableResourceExhaustion("p4_dc_fabric", topo_size="m")
    check = (
        p.oracle_playbook(P4TableResourceExhaustionParams(host_name="leaf_1"))
        .steps[-1]
        .expect
    )
    assert check(comparison(routes, mismatches, pipeline=pipeline)) is expected
    assert not check('{"summary":')
    assert not check("tool_error: read failed")


@pytest.mark.parametrize(
    "mismatches,pipeline,expected",
    [
        (["ipv4_lpm"], True, True),
        ([], True, False),
        (["groups"], True, False),
        (["groups", "ipv4_lpm"], True, False),
        (["ipv4_lpm"], False, False),
    ],
)
def test_partial_write_requires_applied_groups_and_unapplied_routes(
    mismatches, pipeline, expected
):
    p = P4RuntimePartialWrite("p4_dc_fabric", topo_size="m")
    check = (
        p.oracle_playbook(P4RuntimePartialWriteParams(host_name="leaf_1"))
        .steps[-1]
        .expect
    )
    assert check(comparison(11, mismatches, pipeline=pipeline)) is expected
    assert not check('{"summary":')
    assert not check("tool_error: read failed")


@pytest.mark.parametrize("scenario", ["p4_dc_fabric", "p4_dc_gateway"])
@pytest.mark.parametrize("size", ["s", "m", "l"])
@pytest.mark.parametrize("kind", ["missing_member", "wrong_port"])
def test_selector_reference_requires_live_drift_and_specific_member_state(
    scenario, size, kind
):
    cls, params_cls = (
        (P4EcmpGroupMemberMissing, P4EcmpGroupMemberMissingParams)
        if kind == "missing_member"
        else (P4ActionSelectorMemberMisconfig, P4ActionSelectorMemberMisconfigParams)
    )
    problem = cls(scenario, topo_size=size)
    intent = declared_intent(problem.net_env)
    _, group_id, member_id, _ = _ecmp_target(intent, "leaf_1")
    plan = problem.oracle_playbook(params_cls(host_name="leaf_1"))
    checks = [s.expect for s in plan.steps if s.tool == "exec_shell"]
    healthy = intent["switches"]["leaf_1"]
    assert checks and not all(check(json.dumps(healthy)) for check in checks)
    faulty = copy.deepcopy(healthy)
    group = next(g for g in faulty["groups"] if g["group_id"] == group_id)
    member = next(m for m in faulty["members"] if m["member_id"] == member_id)
    if kind == "missing_member":
        group["member_ids"].remove(member_id)
    else:
        member["port"] = _BAD_PORT
    assert all(check(json.dumps(faulty)) for check in checks)
    faulty["members"].remove(member)
    assert not all(check(json.dumps(faulty)) for check in checks)
    assert not any(check("tool_error: read failed") for check in checks)


@pytest.mark.parametrize("scenario", ["p4_dc_fabric", "p4_dc_gateway"])
@pytest.mark.parametrize("size", ["s", "m", "l"])
def test_bounded_route_comparison_retains_context_and_drift_checks(scenario, size):
    problem = P4EcmpGroupMemberMissing(scenario, topo_size=size)
    intent = declared_intent(problem.net_env)
    step = intent_drift("leaf_1", "ipv4_lpm", intent)
    assert not step.evidence
    healthy = intent["switches"]["leaf_1"]
    assert not step.expect(json.dumps(healthy))
    changed = copy.deepcopy(healthy)
    changed["ipv4_lpm"][0]["group_id"] += 1
    assert step.expect(json.dumps(changed))
    assert not step.expect('{"ipv4_lpm":')
