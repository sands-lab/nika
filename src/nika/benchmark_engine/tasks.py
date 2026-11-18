"""Release tasks as a benchmark engine reads them from its own split files.

The engine owns a release's task rows: it normalizes them, computes their
task IDs and resource classes, and turns a task ID into the trial it deploys.
"""

from __future__ import annotations

from typing import Any

from nika.benchmark_engine.protocol import EngineError, TaskInfo, TrialRequest
from nika.workflows.benchmark.admit import resource_class_for_row
from nika.workflows.benchmark.healthy import is_healthy_case
from nika.workflows.benchmark.isp_options import ISP_DEPLOY_KEYS
from nika.workflows.benchmark.multi_fault import row_problems
from nika.workflows.benchmark.resume import benchmark_row_from_case
from nika.workflows.benchmark.trials import (
    index_rows_by_task_id,
    task_id_for_row,
    task_label,
)


def release_task_rows(release: str, split: str) -> dict[str, dict[str, Any]]:
    """The rows of a release split by task ID, in split-file order."""
    from nika.workflows.benchmark.release import load_release

    return index_rows_by_task_id(load_release(release, split=split).cases)


def task_info(row: dict[str, Any]) -> TaskInfo:
    """What ``list_tasks`` reports about one task row."""
    task_id = task_id_for_row(row)
    scenario = str(row["scenario"])
    return TaskInfo(
        task_id=task_id,
        scenario=scenario,
        topo_size=row.get("topo_size") or None,
        resource_class=resource_class_for_row({**row, "case_key": task_id}),
        faults=row_problems(row),
        label=task_label(row),
    )


def resolve_trial(request: TrialRequest) -> TrialRequest:
    """Return *request* with the task row, failures, and ground truth it deploys.

    A ``task_id`` request takes them from the release's split file. A task
    row runs only without a release.
    """
    if request.task_id is None:
        if request.release is not None:
            raise EngineError(
                f"Release {request.release} tasks start by task_id; this engine "
                "does not run a task row for a release."
            )
        return request
    assert request.release is not None and request.split is not None
    row = release_task_rows(request.release, request.split).get(request.task_id)
    if row is None:
        raise EngineError(
            f"Unknown task id {request.task_id!r} in release {request.release} "
            f"split {request.split!r}."
        )
    task = benchmark_row_from_case(
        scenario=row["scenario"],
        problem=row["problem"],
        topo_size=row.get("topo_size") or "",
        inject_params=row["inject"],
        **{key: row.get(key) for key in ISP_DEPLOY_KEYS},
        telemetry=row.get("telemetry"),
        traffic=row.get("traffic"),
        problems=row_problems(row),
        schedule=row.get("schedule"),
        release=request.release,
    )
    return request.model_copy(
        update={
            "task": task,
            "failures": row_problems(task),
            "inject": dict(task.get("inject") or {}),
            "expected_root_causes": None
            if row.get("root_causes_status") == "unresolved"
            else row.get("root_causes"),
            "healthy": is_healthy_case(row["problem"]),
        }
    )
