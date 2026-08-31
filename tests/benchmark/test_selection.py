from __future__ import annotations

from pathlib import Path

import pytest

from nika.problems.registry import list_avail_problem_instances
from nika.workflows.benchmark.candidate_context import (
    collapse_candidates,
    deployment_environment_key,
    enrich_candidate,
)
from nika.workflows.benchmark.coverage_report import build_coverage_report
from nika.workflows.benchmark.healthy import HEALTHY_PROBLEM
from nika.workflows.benchmark.load_config import load_candidate_catalog
from nika.workflows.benchmark.selection import (
    baseline_one_per_failure,
    compute_gain,
    select_benchmark_cases,
    selection_context_key,
    selection_fingerprints,
    CoverageState,
)
from nika.workflows.benchmark.split_catalog import (
    effective_backend,
    select_dev_test_cases,
    validate_dev_test_split,
)


def _synthetic_pool() -> list[dict]:
    scenarios = ("dc_clos", "campus_lan", "enterprise_branch")
    failures = ("link_down", "link_flap", "bgp_hijacking")
    pool: list[dict] = []
    option = 0
    for failure in failures:
        for scenario in scenarios:
            for scale in ("s", "m"):
                option += 1
                pool.append(
                    enrich_candidate(
                        {
                            "scenario": scenario,
                            "problem": failure,
                            "topo_size": scale,
                            "inject": {"host_name": "host_a", "intf_name": "eth0"},
                            "root_causes": [
                                {
                                    "resource": {
                                        "kind": "interface",
                                        "node": "host_a",
                                        "name": "eth0",
                                    },
                                    "fault_type": failure,
                                }
                            ],
                            "candidate_option_id": f"opt-{option:04d}",
                        }
                    )
                )
    for scenario in scenarios:
        pool.append(
            enrich_candidate(
                {
                    "scenario": scenario,
                    "problem": HEALTHY_PROBLEM,
                    "topo_size": "s",
                    "inject": {},
                    "root_causes": [],
                    "candidate_option_id": f"healthy-{scenario}",
                }
            )
        )
    return pool


def test_selection_prefers_opposite_backend_for_second_env() -> None:
    pool = [
        enrich_candidate(
            {
                "scenario": "isp_abilene",
                "problem": "link_down",
                "topo_size": "s",
                "igp": "isis",
                "bgp_mode": "ibgp_rr",
                "rpki": False,
                "backend": "kathara",
                "device_profile": "frr",
                "inject": {"host_name": "Atlanta", "intf_name": "eth1"},
                "candidate_option_id": "link-kathara",
            }
        ),
        enrich_candidate(
            {
                "scenario": "isp_abilene",
                "problem": "link_down",
                "topo_size": "s",
                "igp": "isis",
                "bgp_mode": "ibgp_rr",
                "rpki": False,
                "backend": "containerlab",
                "device_profile": "nokia_srlinux",
                "inject": {"host_name": "Atlanta", "intf_name": "e1-1"},
                "candidate_option_id": "link-clab",
            }
        ),
        enrich_candidate(
            {
                "scenario": "isp_polska",
                "problem": "link_down",
                "topo_size": "s",
                "igp": "isis",
                "bgp_mode": "ibgp_rr",
                "rpki": False,
                "backend": "kathara",
                "device_profile": "frr",
                "inject": {"host_name": "Warsaw", "intf_name": "eth1"},
                "candidate_option_id": "link-polska-kathara",
            }
        ),
        enrich_candidate(
            {
                "scenario": "isp_abilene",
                "problem": HEALTHY_PROBLEM,
                "topo_size": "s",
                "igp": "isis",
                "bgp_mode": "ibgp_rr",
                "rpki": False,
                "backend": "kathara",
                "device_profile": "frr",
                "inject": {},
                "candidate_option_id": "healthy-abilene-kathara",
            }
        ),
        enrich_candidate(
            {
                "scenario": "isp_abilene",
                "problem": HEALTHY_PROBLEM,
                "topo_size": "s",
                "igp": "isis",
                "bgp_mode": "ibgp_rr",
                "rpki": False,
                "backend": "containerlab",
                "device_profile": "nokia_srlinux",
                "inject": {},
                "candidate_option_id": "healthy-abilene-clab",
            }
        ),
        enrich_candidate(
            {
                "scenario": "isp_polska",
                "problem": HEALTHY_PROBLEM,
                "topo_size": "s",
                "igp": "isis",
                "bgp_mode": "ibgp_rr",
                "rpki": False,
                "backend": "kathara",
                "device_profile": "frr",
                "inject": {},
                "candidate_option_id": "healthy-polska-kathara",
            }
        ),
    ]
    cases, _ = select_benchmark_cases(pool, seed=42)
    link_cases = [row for row in cases if row["problem"] == "link_down"]
    backends = {str(row.get("backend") or "") for row in link_cases}
    assert backends == {"kathara", "containerlab"}
    assert len(link_cases) == 2


def test_selection_covers_all_failures_and_healthy_scenarios() -> None:
    pool = _synthetic_pool()
    cases, state = select_benchmark_cases(pool, seed=42)
    failures = {row["problem"] for row in cases if row["problem"] != HEALTHY_PROBLEM}
    assert failures == {"link_down", "link_flap", "bgp_hijacking"}
    failure_environments = {
        deployment_environment_key(row)
        for row in cases
        if row["problem"] != HEALTHY_PROBLEM
    }
    assert state.healthy_environment == failure_environments


def test_duplicate_failure_requires_distinct_context() -> None:
    pool = _synthetic_pool()
    cases, _state = select_benchmark_cases(pool, seed=42)
    by_failure: dict[str, list[tuple]] = {}
    for row in cases:
        if row["problem"] == HEALTHY_PROBLEM:
            continue
        by_failure.setdefault(row["problem"], []).append(selection_context_key(row))
    for problem, items in by_failure.items():
        assert len(items) == len(set(items)), problem


def test_selection_is_reproducible_with_seed() -> None:
    pool = _synthetic_pool()
    cases_a, _ = select_benchmark_cases(pool, seed=7)
    cases_b, _ = select_benchmark_cases(pool, seed=7)
    assert selection_fingerprints(cases_a) == selection_fingerprints(cases_b)


def test_selection_rejects_failure_without_matched_healthy_control() -> None:
    pool = [
        enrich_candidate(
            {
                "scenario": "dc_clos",
                "problem": "link_down",
                "topo_size": "m",
                "inject": {"host_name": "host_a", "intf_name": "eth0"},
                "candidate_option_id": "failure-m",
            }
        ),
        enrich_candidate(
            {
                "scenario": "dc_clos",
                "problem": HEALTHY_PROBLEM,
                "topo_size": "s",
                "inject": {},
                "root_causes": [],
                "candidate_option_id": "healthy-s",
            }
        ),
    ]

    with pytest.raises(ValueError, match="no environment-matched healthy control"):
        select_benchmark_cases(pool, seed=42)


def test_selected_smaller_than_pool() -> None:
    pool = _synthetic_pool()
    cases, _ = select_benchmark_cases(pool, seed=42)
    assert len(cases) < len(pool)


def test_coverage_beats_baseline_on_synthetic_pool() -> None:
    pool = _synthetic_pool()
    cases, _ = select_benchmark_cases(pool, seed=42)
    report = build_coverage_report(selected_cases=cases, pool_candidates=pool)
    comparison = report["comparison_vs_baseline"]
    assert comparison["failure_scenario_pairs_delta"] >= 0
    assert comparison["scenario_scale_pairs_delta"] >= 0
    assert report["selection_contract"]["passed"] is True
    assert report["healthy_control_coverage"]["coverage_rate"] == 1.0


def test_compute_gain_rejects_duplicate_context() -> None:
    pool = _synthetic_pool()
    state = CoverageState()
    first = next(row for row in pool if row["problem"] == "link_down")
    state.add(first)
    gain = compute_gain(first, state)
    assert gain < 0


def test_semantic_isolation_rejects_different_targets_in_same_context() -> None:
    base = {
        "scenario": "dc_clos",
        "problem": "link_down",
        "topo_size": "s",
    }
    dev = [{**base, "inject": {"host_name": "pc_0_0", "intf_name": "eth0"}}]
    test = [{**base, "inject": {"host_name": "pc_0_1", "intf_name": "eth0"}}]
    with pytest.raises(ValueError, match="semantic isolation"):
        validate_dev_test_split(dev, test)


def test_baseline_one_per_failure_count() -> None:
    pool = _synthetic_pool()
    baseline = baseline_one_per_failure(pool)
    assert len(baseline) == 3


def test_real_catalog_selection_constraints() -> None:
    catalog = Path("benchmark/working/pool")
    if not catalog.is_dir():
        return
    collapsed = collapse_candidates(load_candidate_catalog(catalog))
    cases, _state = select_benchmark_cases(collapsed, seed=42)
    registry = {cls.root_cause_name for cls in list_avail_problem_instances().values()}
    selected_failures = {
        row["problem"] for row in cases if row["problem"] != HEALTHY_PROBLEM
    }
    assert registry <= selected_failures
    assert len(cases) < len(collapsed)
    healthy_environments = {
        deployment_environment_key(row)
        for row in cases
        if row["problem"] == HEALTHY_PROBLEM
    }
    failure_environments_by_problem: dict[str, set[tuple]] = {}
    feasible_environments_by_problem: dict[str, set[tuple]] = {}
    pool_healthy_environments = {
        deployment_environment_key(row)
        for row in collapsed
        if row["problem"] == HEALTHY_PROBLEM
    }
    for row in collapsed:
        if row["problem"] == HEALTHY_PROBLEM:
            continue
        environment = deployment_environment_key(row)
        if environment in pool_healthy_environments:
            feasible_environments_by_problem.setdefault(row["problem"], set()).add(
                environment
            )
    for row in cases:
        if row["problem"] == HEALTHY_PROBLEM:
            continue
        environment = deployment_environment_key(row)
        failure_environments_by_problem.setdefault(row["problem"], set()).add(
            environment
        )
        assert environment in healthy_environments
    for problem, feasible in feasible_environments_by_problem.items():
        assert len(failure_environments_by_problem[problem]) == min(2, len(feasible))
    scales = {row.get("topo_size") or "fixed" for row in cases}
    assert {"s", "m", "l"} & scales
    for row in cases:
        assert "scenario" in row and "problem" in row


def test_real_catalog_dev_test_split_contract() -> None:
    catalog = Path("benchmark/working/pool")
    if not catalog.is_dir():
        return
    collapsed = collapse_candidates(load_candidate_catalog(catalog))
    dev, test = select_dev_test_cases(collapsed, seed=42)
    registry = set(list_avail_problem_instances())
    validate_dev_test_split(dev, test, expected_failures=registry)
    test_healthy = sum(1 for row in test if row["problem"] == HEALTHY_PROBLEM)
    assert 0.10 <= test_healthy / len(test) <= 0.20
    assert {
        row["problem"] for row in dev if row["problem"] != HEALTHY_PROBLEM
    } == registry
    assert {
        row["problem"] for row in test if row["problem"] != HEALTHY_PROBLEM
    } == registry
    assert {effective_backend(row) for row in dev} == {"kathara", "containerlab"}
    assert {effective_backend(row) for row in test} == {"kathara", "containerlab"}
    assert all(effective_backend(row) for row in dev + test)
