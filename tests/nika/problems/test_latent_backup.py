"""Contracts for independently routed standby and primary fault targets."""

from pathlib import Path

import pytest

from nika.problems.rca.inventory import load_offline_net_env, link_containing_endpoint
from nika.problems.registry import get_problem_class
from nika.workflows.benchmark.inject_resolve import (
    resolve_multi_inject_params,
    validate_benchmark_case,
)
from nika.workflows.benchmark.load_config import load_benchmark_yaml
from nika.workflows.benchmark.migrate import materialize_case

KEY = "wireguard_peer_key_misconfiguration"
PROBLEMS = [KEY, "link_down"]
DEFINITION = Path(__file__).parents[3] / "benchmark/experiments/latent_backup.yaml"
pytestmark = pytest.mark.contract


@pytest.mark.parametrize("profile", ["frr", "routeros"])
def test_failover_pair_selects_the_same_branch_and_independent_provider(profile):
    options = {"device_profile": profile}
    params = resolve_multi_inject_params(
        PROBLEMS, "enterprise_branch", "s", seed=42, isp_options=options
    )
    assert params == resolve_multi_inject_params(
        PROBLEMS, "enterprise_branch", "s", seed=42, isp_options=options
    )
    env = load_offline_net_env("enterprise_branch", "s", **options)
    key, link = params[KEY], params["link_down"]
    standby = next(
        t
        for t in env.built_tunnels
        if f"{t.spoke}_edge" == key["host_name"] and t.spoke_iface == key["intf_name"]
    )
    primary = next(
        t for t in env.built_tunnels if t.spoke == standby.spoke and t.primary
    )
    assert not standby.primary and standby.provider != primary.provider
    primary_wan = env._edges[primary.spoke].wan[primary.provider][3]
    assert link_containing_endpoint(
        env, link["host_name"], link["intf_name"]
    ) == link_containing_endpoint(env, key["host_name"], primary_wan)
    for name in PROBLEMS:
        validate_benchmark_case(
            "enterprise_branch", name, params[name], "s", isp_options=options
        )
    definition = next(
        row
        for row in load_benchmark_yaml(DEFINITION)
        if row.get("device_profile", "frr") == profile
    )
    assert definition["inject"] == params
    causes = materialize_case(definition)["root_causes"]
    assert causes == definition["root_causes"]
    assert len(causes) == 2


def test_oracle_partial_submission_scores_one_of_two_causes():
    from nika.agents.oracle.agent import FaultVerdict, OracleAgent, StepResult
    from nika.problems.rca.models import RootCause

    causes = materialize_case(load_benchmark_yaml(DEFINITION)[0])["root_causes"]
    pairs = [
        {
            "resource_id": RootCause.model_validate(c).resource_id,
            "fault_type": c["fault_type"],
        }
        for c in causes
    ]
    from nika.workflows.eval.session import build_eval_metrics_payload

    agent = OracleAgent.__new__(OracleAgent)
    agent.verdicts = [
        FaultVerdict(
            name,
            "",
            True,
            [cause],
            steps=[StepResult(1, "", "exec_shell", {}, True, None, index == 0, 1, "")],
        )
        for index, (name, cause) in enumerate(zip(PROBLEMS, pairs, strict=True))
    ]
    submission = agent._submission(
        {"resources": [{"id": c["resource_id"]} for c in pairs]}
    )
    assert len(submission["root_causes"]) == 1
    metrics, status = build_eval_metrics_payload(
        gt={"is_anomaly": True, "root_causes": causes},
        submission=submission,
        trace_metrics={},
    )
    assert status == "scored"
    assert metrics["detection_score"] == metrics["rca_precision"] == 1.0
    assert metrics["rca_accuracy"] == 0.5


def test_peer_key_scope_preserves_existing_branch_ground_truth():
    from nika.problems.rca.materialize import ground_truth_for_case

    params = {"host_name": "br1_edge", "intf_name": "wg_hq"}
    branch = ground_truth_for_case(
        problem=KEY, scenario="enterprise_branch", topo_size="s", params=params
    )
    selected = ground_truth_for_case(
        problem=KEY,
        scenario="enterprise_branch",
        topo_size="s",
        params={**params, "scope": "interface"},
    )
    assert {cause.resource_id for cause in branch.root_causes} == {
        "interface/br1_edge/wg_hq",
        "interface/br1_edge/wg_hq_b",
        "interface/br1_edge/wg_dc2",
    }
    assert {cause.resource_id for cause in selected.root_causes} == {
        "interface/br1_edge/wg_hq"
    }
    assert get_problem_class(KEY).Params(**params).scope == "branch"
