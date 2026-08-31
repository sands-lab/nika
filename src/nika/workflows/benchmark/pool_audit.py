"""Static audit of benchmark candidate catalogs."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from nika.problems.rca.materialize import assert_root_causes_match, ground_truth_for_case
from nika.problems.rca.inventory import load_offline_net_env
from nika.problems.registry import list_avail_problem_instances
from nika.workflows.benchmark.candidate_context import (
    MAJOR_SCENARIOS,
    collapse_candidates,
    normalize_topo_scale,
    pool_context_key,
)
from nika.workflows.benchmark.healthy import is_healthy_case
from nika.workflows.benchmark.load_config import load_candidate_catalog, normalize_benchmark_row


def _has_recover_fault(problem: str) -> bool:
    if is_healthy_case(problem):
        return True
    cls = list_avail_problem_instances().get(problem)
    if cls is None:
        return False
    return callable(getattr(cls, "recover_fault", None))


def _telemetry_flags(problem: str) -> list[str]:
    if is_healthy_case(problem):
        return []
    flags: list[str] = []
    cls = list_avail_problem_instances().get(problem)
    if cls is None:
        return ["unknown_failure"]
    description = str(getattr(cls, "description", "") or "")
    symptom = str(getattr(cls, "symptom_desc", "") or "")
    combined = f"{description} {symptom}".lower()
    for token in ("inject", "ground truth", "root cause", "fault_type"):
        if token in combined:
            flags.append(f"description_contains_{token.replace(' ', '_')}")
    if not flags:
        flags.append("telemetry_review_needed")
    return flags


def _ground_truth_check(row: dict[str, Any], env_cache: dict[tuple[Any, ...], Any]) -> None:
    scenario = str(row["scenario"])
    topo_size = str(row.get("topo_size") or "")
    isp_kwargs: dict[str, Any] = {}
    for key in ("topo", "igp", "bgp_mode", "rpki", "backend", "device_profile"):
        if key in row:
            isp_kwargs[key] = row[key]
    cache_key = (
        scenario,
        topo_size,
        str(isp_kwargs.get("topo") or ""),
        str(isp_kwargs.get("igp") or ""),
        str(isp_kwargs.get("bgp_mode") or ""),
        str(isp_kwargs.get("rpki", False)),
        str(isp_kwargs.get("backend") or ""),
        str(isp_kwargs.get("device_profile") or ""),
    )
    if cache_key not in env_cache:
        env_cache[cache_key] = load_offline_net_env(scenario, topo_size, **isp_kwargs)
    truth = ground_truth_for_case(
        problem=str(row["problem"]),
        params=dict(row.get("inject") or {}),
        scenario=scenario,
        topo_size=topo_size,
        net_env=env_cache[cache_key],
    )
    expected = row.get("root_causes") or []
    assert_root_causes_match(truth, expected)


def audit_candidate_pool(path: str) -> dict[str, Any]:
    """Audit a candidate catalog and return a structured report."""
    raw_rows = load_candidate_catalog(path)
    registry_failures = set(list_avail_problem_instances())
    env_cache: dict[tuple[Any, ...], Any] = {}

    row_issues: list[dict[str, Any]] = []
    passed_rows = 0
    failure_problems = set()
    scenarios_seen: set[str] = set()
    scale_counts: Counter[str] = Counter()
    failure_scenario: dict[str, set[str]] = defaultdict(set)

    for index, row in enumerate(raw_rows):
        problem = str(row["problem"])
        scenarios_seen.add(str(row["scenario"]))
        scale_counts[normalize_topo_scale(row)] += 1
        if not is_healthy_case(problem):
            failure_problems.add(problem)
            failure_scenario[problem].add(str(row["scenario"]))

        issues: list[str] = []
        warnings: list[str] = []
        try:
            normalize_benchmark_row(row)
        except ValueError as exc:
            issues.append(f"normalize_failed: {exc}")

        if not is_healthy_case(problem):
            inject = row.get("inject") or {}
            if not inject:
                issues.append("inject_empty")
            warnings.extend(f"telemetry:{flag}" for flag in _telemetry_flags(problem))

        if issues:
            row_issues.append(
                {
                    "index": index,
                    "option_id": row.get("candidate_option_id"),
                    "scenario": row.get("scenario"),
                    "problem": problem,
                    "issues": issues,
                    "warnings": warnings,
                }
            )
        else:
            passed_rows += 1
            if warnings:
                row_issues.append(
                    {
                        "index": index,
                        "option_id": row.get("candidate_option_id"),
                        "scenario": row.get("scenario"),
                        "problem": problem,
                        "issues": [],
                        "warnings": warnings,
                    }
                )

    collapsed = collapse_candidates(raw_rows)
    gt_issues: list[dict[str, Any]] = []
    gt_passed = 0
    for row in collapsed:
        if is_healthy_case(row.get("problem")):
            gt_passed += 1
            continue
        try:
            _ground_truth_check(row, env_cache)
            gt_passed += 1
        except Exception as exc:  # noqa: BLE001
            gt_issues.append(
                {
                    "context": pool_context_key(row),
                    "option_id": row.get("candidate_option_id"),
                    "error": str(exc),
                }
            )

    recover_missing = sorted(
        problem
        for problem in registry_failures
        if not _has_recover_fault(problem)
    )

    static_failures = [item for item in row_issues if item.get("issues")]
    return {
        "summary": {
            "total_rows": len(raw_rows),
            "passed_static_rows": passed_rows,
            "failed_static_rows": len(static_failures),
            "collapsed_contexts": len(collapsed),
            "ground_truth_passed_contexts": gt_passed,
            "ground_truth_failed_contexts": len(gt_issues),
            "unique_failures_in_pool": len(failure_problems),
            "registry_failures": len(registry_failures),
            "missing_failures": sorted(registry_failures - failure_problems),
            "scenarios": len(scenarios_seen),
            "major_scenarios_present": sorted(set(MAJOR_SCENARIOS) & scenarios_seen),
            "major_scenarios_missing": sorted(set(MAJOR_SCENARIOS) - scenarios_seen),
            "recover_fault_missing_failures": recover_missing,
        },
        "scale_counts": dict(sorted(scale_counts.items())),
        "failure_scenario_counts": {
            problem: len(scenarios)
            for problem, scenarios in sorted(failure_scenario.items())
        },
        "row_issues": row_issues[:200],
        "row_issues_truncated": max(0, len(row_issues) - 200),
        "ground_truth_issues": gt_issues[:200],
        "ground_truth_issues_truncated": max(0, len(gt_issues) - 200),
        "eligible_for_selection": (
            len(static_failures) == 0 and len(gt_issues) == 0
        ),
    }


def eligible_candidates(path: str) -> list[dict[str, Any]]:
    """Return collapsed, audit-passing candidates for selection."""
    report = audit_candidate_pool(path)
    summary = report["summary"]
    if not report["eligible_for_selection"]:
        raise ValueError(
            "Candidate pool failed audit: "
            f"static_failures={summary['failed_static_rows']}, "
            f"gt_failures={summary['ground_truth_failed_contexts']}"
        )
    if summary["missing_failures"]:
        raise ValueError(
            "Candidate pool is missing registry failures: "
            f"{summary['missing_failures']}"
        )
    return collapse_candidates(load_candidate_catalog(path))
