"""Coverage summary reports for selected benchmark subsets."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from nika.workflows.benchmark.candidate_context import (
    MAJOR_SCENARIOS,
    deployment_environment_key,
    enrich_candidate,
    normalize_topo_scale,
)
from nika.workflows.benchmark.healthy import is_healthy_case
from nika.workflows.benchmark.selection import baseline_one_per_failure


def _matrix_from_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    failure_cases = [row for row in cases if not is_healthy_case(row.get("problem"))]
    healthy_cases = [row for row in cases if is_healthy_case(row.get("problem"))]
    failure_counts = Counter(str(row["problem"]) for row in failure_cases)
    scenario_counts = Counter(str(row["scenario"]) for row in cases)
    scale_counts = Counter(normalize_topo_scale(row) for row in cases)
    failure_scenario: set[tuple[str, str]] = set()
    failure_environment: set[tuple[Any, ...]] = set()
    scenario_scale: set[tuple[str, str]] = set()
    domain_scale: set[tuple[str, str]] = set()
    for row in failure_cases:
        problem = str(row["problem"])
        scenario = str(row["scenario"])
        scale = normalize_topo_scale(row)
        failure_scenario.add((problem, scenario))
        failure_environment.add((problem, *deployment_environment_key(row)))
        scenario_scale.add((scenario, scale))
        domain = enrich_candidate(row).get("failure_domain") or ""
        if domain:
            domain_scale.add((str(domain), scale))
    return {
        "total_cases": len(cases),
        "failure_cases": len(failure_cases),
        "healthy_cases": len(healthy_cases),
        "failure_pick_counts": dict(sorted(failure_counts.items())),
        "scenario_counts": dict(sorted(scenario_counts.items())),
        "scale_counts": dict(sorted(scale_counts.items())),
        "failure_scenario_pairs": len(failure_scenario),
        "failure_environment_pairs": len(failure_environment),
        "healthy_environment_controls": len(
            {deployment_environment_key(row) for row in healthy_cases}
        ),
        "scenario_scale_pairs": len(scenario_scale),
        "failure_domain_scale_pairs": len(domain_scale),
    }


def _feasible_pairs(pool: list[dict[str, Any]]) -> dict[str, set[tuple[str, str]]]:
    failure_scenario: set[tuple[str, str]] = set()
    scenario_scale: set[tuple[str, str]] = set()
    domain_scale: set[tuple[str, str]] = set()
    for row in pool:
        enriched = enrich_candidate(row)
        if enriched.get("is_healthy"):
            scenario_scale.add((str(row["scenario"]), enriched["topo_scale"]))
            continue
        failure_scenario.add((str(row["problem"]), str(row["scenario"])))
        scenario_scale.add((str(row["scenario"]), enriched["topo_scale"]))
        domain = enriched.get("failure_domain") or ""
        if domain:
            domain_scale.add((str(domain), enriched["topo_scale"]))
    return {
        "failure_scenario": failure_scenario,
        "scenario_scale": scenario_scale,
        "failure_domain_scale": domain_scale,
    }


def _uncovered(
    selected_pairs: set[tuple[str, str]],
    feasible: set[tuple[str, str]],
) -> list[list[str]]:
    missing = sorted(feasible - selected_pairs)
    return [list(pair) for pair in missing]


def build_coverage_report(
    *,
    selected_cases: list[dict[str, Any]],
    pool_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    pool = [enrich_candidate(row) for row in pool_candidates]
    feasible = _feasible_pairs(pool)
    selected_summary = _matrix_from_cases(selected_cases)

    selected_failure_scenario = {
        (str(row["problem"]), str(row["scenario"]))
        for row in selected_cases
        if not is_healthy_case(row.get("problem"))
    }
    selected_failure_environments_by_problem: dict[str, set[tuple[Any, ...]]] = (
        defaultdict(set)
    )
    selected_failure_environments: set[tuple[Any, ...]] = set()
    selected_healthy_environments: set[tuple[Any, ...]] = set()
    for row in selected_cases:
        environment = deployment_environment_key(row)
        if is_healthy_case(row.get("problem")):
            selected_healthy_environments.add(environment)
        else:
            problem = str(row["problem"])
            selected_failure_environments_by_problem[problem].add(environment)
            selected_failure_environments.add(environment)

    healthy_pool_environments = {
        deployment_environment_key(row)
        for row in pool
        if is_healthy_case(row.get("problem"))
    }
    feasible_controlled_environments: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
    for row in pool:
        if is_healthy_case(row.get("problem")):
            continue
        environment = deployment_environment_key(row)
        if environment in healthy_pool_environments:
            feasible_controlled_environments[str(row["problem"])].add(environment)

    repeatable_failures = sorted(
        problem
        for problem, environments in feasible_controlled_environments.items()
        if len(environments) >= 2
    )
    single_environment_failures = sorted(
        problem
        for problem, environments in feasible_controlled_environments.items()
        if len(environments) == 1
    )
    repetition_violations = [
        problem
        for problem in repeatable_failures
        if len(selected_failure_environments_by_problem.get(problem, set())) < 2
    ]
    missing_healthy_controls = sorted(
        selected_failure_environments - selected_healthy_environments
    )
    matched_healthy = selected_failure_environments & selected_healthy_environments
    all_failures_covered = not (
        set(feasible_controlled_environments)
        - set(selected_failure_environments_by_problem)
    )
    selected_scenario_scale = {
        (str(row["scenario"]), normalize_topo_scale(row)) for row in selected_cases
    }
    selected_domain_scale = set()
    for row in selected_cases:
        if is_healthy_case(row.get("problem")):
            continue
        enriched = enrich_candidate(row)
        domain = enriched.get("failure_domain") or ""
        if domain:
            selected_domain_scale.add((str(domain), enriched["topo_scale"]))

    baseline_summary = _matrix_from_cases(baseline_one_per_failure(pool_candidates))

    def _rate(summary: dict[str, Any], feasible_count: int, key: str) -> float:
        if feasible_count == 0:
            return 0.0
        return round(summary.get(key, 0) / feasible_count, 4)

    feasible_fs = len(feasible["failure_scenario"])
    feasible_ss = len(feasible["scenario_scale"])
    feasible_ds = len(feasible["failure_domain_scale"])

    return {
        "summary": selected_summary,
        "failure_scenario_coverage": {
            "selected_pairs": selected_summary["failure_scenario_pairs"],
            "feasible_pairs": feasible_fs,
            "coverage_rate": _rate(
                selected_summary, feasible_fs, "failure_scenario_pairs"
            ),
            "uncovered_feasible": _uncovered(
                selected_failure_scenario, feasible["failure_scenario"]
            ),
        },
        "cross_environment_repetition": {
            "environment_key": [
                "scenario",
                "topo_scale",
                "igp",
                "bgp_mode",
                "rpki",
            ],
            "repeatable_failures": len(repeatable_failures),
            "satisfied_failures": len(repeatable_failures) - len(repetition_violations),
            "single_environment_failures": single_environment_failures,
            "violations": repetition_violations,
        },
        "healthy_control_coverage": {
            "selected_failure_environments": len(selected_failure_environments),
            "matched_healthy_environments": len(matched_healthy),
            "coverage_rate": round(
                len(matched_healthy) / len(selected_failure_environments),
                4,
            )
            if selected_failure_environments
            else 0.0,
            "missing_environments": [list(item) for item in missing_healthy_controls],
        },
        "scenario_scale_coverage": {
            "selected_pairs": selected_summary["scenario_scale_pairs"],
            "feasible_pairs": feasible_ss,
            "coverage_rate": _rate(
                selected_summary, feasible_ss, "scenario_scale_pairs"
            ),
            "uncovered_feasible": _uncovered(
                selected_scenario_scale, feasible["scenario_scale"]
            ),
        },
        "failure_domain_scale_coverage": {
            "selected_pairs": selected_summary["failure_domain_scale_pairs"],
            "feasible_pairs": feasible_ds,
            "coverage_rate": _rate(
                selected_summary, feasible_ds, "failure_domain_scale_pairs"
            ),
            "uncovered_feasible": _uncovered(
                selected_domain_scale, feasible["failure_domain_scale"]
            ),
        },
        "baseline_one_failure_one_case": {
            "summary": baseline_summary,
            "failure_scenario_coverage_rate": _rate(
                baseline_summary, feasible_fs, "failure_scenario_pairs"
            ),
            "scenario_scale_coverage_rate": _rate(
                baseline_summary, feasible_ss, "scenario_scale_pairs"
            ),
        },
        "comparison_vs_baseline": {
            "failure_scenario_pairs_delta": (
                selected_summary["failure_scenario_pairs"]
                - baseline_summary["failure_scenario_pairs"]
            ),
            "scenario_scale_pairs_delta": (
                selected_summary["scenario_scale_pairs"]
                - baseline_summary["scenario_scale_pairs"]
            ),
            "total_cases_delta": (
                selected_summary["total_cases"] - baseline_summary["total_cases"]
            ),
        },
        "major_scenarios": {
            scenario: selected_summary["scenario_counts"].get(scenario, 0)
            for scenario in MAJOR_SCENARIOS
        },
        "selection_contract": {
            "all_failures_covered": all_failures_covered,
            "cross_environment_repetition_satisfied": not repetition_violations,
            "healthy_controls_matched": not missing_healthy_controls,
            "passed": (
                all_failures_covered
                and not repetition_violations
                and not missing_healthy_controls
            ),
        },
    }
