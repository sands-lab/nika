"""Shared helpers for failure inject contract tests."""

from __future__ import annotations

from nika.utils.session_store import SessionStore
from nika.workflows.failure.inject import inject_failure as inject_failure_workflow
from tests.benchmark.helpers import inject_params_from_benchmark_yaml


def resolve_inject_params(
    scenario: str,
    problem: str,
    *,
    topo_size: str = "",
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    try:
        params = inject_params_from_benchmark_yaml(scenario, problem, topo_size)
    except ValueError:
        from benchmark.inject_resolve import resolve_inject_params as bench_resolve

        params = bench_resolve(problem, scenario, topo_size, seed=42)
    if overrides:
        params.update(overrides)
    return params


def inject_and_assert_ground_truth(
    session_id: str,
    scenario: str,
    problem: str,
    params: dict[str, str],
) -> None:
    inject_failure_workflow([problem], session_id=session_id, param_overrides=params)
    failures = SessionStore().list_failure_injections(session_id=session_id)
    matching = [row for row in failures if row.get("problem_name") == problem]
    assert matching, f"No failure record for {problem}"
    assert matching[-1].get("status") == "injected"

    session_dir = SessionStore().get_session(session_id)["session_dir"]
    from pathlib import Path
    import json

    gt_path = Path(session_dir) / "ground_truth.json"
    assert gt_path.is_file(), "ground_truth.json must exist after inject"
    ground_truth = json.loads(gt_path.read_text(encoding="utf-8"))
    assert ground_truth.get("is_anomaly") is True
    assert ground_truth.get("root_causes"), f"missing root_causes for {problem}"
    fault_types = {
        item.get("fault_type") for item in ground_truth.get("root_causes") or []
    }
    assert problem in fault_types
