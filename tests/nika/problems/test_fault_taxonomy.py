"""Fault eligibility, repeated placements, and symmetric evidence contracts."""

import pytest

from nika.evaluator.scoring import score_rca
from nika.problems.combinations import get_combination
from nika.problems.rca.materialize import (
    ground_truth_for_multi_case,
    ground_truth_for_case,
)
from nika.problems.registry import get_problem_class
from nika.workflows.benchmark.inject_resolve import (
    resolve_inject_params,
    validate_benchmark_case,
)
from nika.workflows.benchmark.load_config import normalize_benchmark_row
from nika.workflows.benchmark.selection import select_benchmark_cases

pytestmark = pytest.mark.contract


@pytest.mark.parametrize(
    "name", ["bgp_max_prefix_limit_low", "bgp_prefix_surge", "bgp_rov_not_enforced"]
)
def test_combination_members_reject_single_fault_tasks(name):
    row = {
        "scenario": "isp_abilene",
        "problem": name,
        "igp": "ospf",
        "bgp_mode": "ebgp",
        "inject": {"host_name": "iplsng"},
    }
    assert get_problem_class(name).combination_only
    with pytest.raises(ValueError, match="only in multi-fault"):
        normalize_benchmark_row(row)
    with pytest.raises(ValueError, match="only in multi-fault"):
        select_benchmark_cases([row])
    with pytest.raises(ValueError, match="only in multi-fault"):
        validate_benchmark_case("isp_abilene", name, row["inject"])


def test_redundant_link_ground_truth_contains_both_placements():
    row = get_combination("redundant_links_both_lost").rows()[0]
    truth = ground_truth_for_multi_case(
        problems=row["problems"],
        params=row["inject"],
        scenario=row["scenario"],
        topo_size="s",
    )
    assert len(truth.root_causes) == 2
    assert {cause.fault_type for cause in truth.root_causes} == {"link_down"}
    assert len({cause.resource_id for cause in truth.root_causes}) == 2


@pytest.mark.parametrize(
    "name",
    [
        "ospf_duplicate_router_id",
        "ospf_network_type_mismatch",
        "ospf_hello_interval_mismatch",
        "mac_address_conflict",
    ],
)
def test_symmetric_evidence_accepts_either_endpoint(name):
    params = resolve_inject_params(name, "campus_lan", "s")
    truth = ground_truth_for_case(
        problem=name, params=params, scenario="campus_lan", topo_size="s"
    ).model_dump(mode="json")
    assert len(truth["root_causes"]) == 2
    for cause in truth["root_causes"]:
        submission = {"is_anomaly": True, "root_causes": [cause]}
        assert score_rca(submission, truth)["rca_f1"] == 1.0


def test_host_ip_conflict_requires_an_independent_l2_observer():
    from nika.workflows.benchmark.generate import build_failure_cases

    cases, rejected = build_failure_cases(
        problem="host_ip_conflict",
        scenario="enterprise_branch",
        topo_size="s",
        isp_options=None,
    )
    assert cases == []
    assert rejected


def test_dns_port_blocking_keeps_non_kubernetes_parameter_resolution():
    from types import SimpleNamespace

    problem = get_problem_class("dns_port_blocked")(None)
    problem.net_env = SimpleNamespace(kubernetes_nodes=[])
    params = problem.resolve_params({"host_name": "dns_server"})
    assert params.host_name == "dns_server"
    assert params.control_node is None


@pytest.mark.parametrize("scenario", ["p4_dc_gateway", "p4_dc_fabric"])
def test_capacity_bottleneck_covers_the_declared_fan_in_receiver(scenario):
    from nika.problems.rca.inventory import load_offline_net_env
    from nika.traffic.workload import FAN_IN_BURSTS
    from nika.workflows.benchmark.generate import build_failure_cases

    env = load_offline_net_env(scenario, "s")
    cases, _ = build_failure_cases(
        problem="link_capacity_bottleneck",
        scenario=scenario,
        topo_size="s",
        isp_options=None,
    )
    assert cases
    from nika.problems.link_interface.link import _link_peer

    for case in cases:
        params = case["inject"]
        peer = _link_peer(env, params["host_name"], params["intf_name"])
        assert env.business_workload().covers(FAN_IN_BURSTS, (peer[0],))
