"""Release tasks as this engine reads them from its own split files.

A task ID is the ``case_key`` this release's runner gave the task, so trial
directories keep the names release 0.1.0 runs used. A task ID resolves to the
row the runner deployed, with the same failure, injection parameters, and
expected root causes.
"""

from __future__ import annotations

from typing import Any

from nika.benchmark_engine.protocol import EngineError, TaskInfo, TrialRequest
from nika.workflows.benchmark.trials import case_key_for_row

# Admission classes the frontend schedules by. Release 0.1.0 deploys only
# Kathara labs, so size ``l`` is the only heavy one.
CLASS_LIGHT = "light"
CLASS_LARGE = "large"

_INJECT_LABEL_KEYS = ("host_name", "host_name_2", "intf_name", "router_name")
_INJECT_KEY_SHORT = {
    "host_name": "host",
    "host_name_2": "host2",
    "intf_name": "intf",
    "router_name": "router",
}


def release_task_rows(release: str, split: str) -> dict[str, dict[str, Any]]:
    """The rows of a release split by task ID, in split-file order."""
    from nika.workflows.benchmark.release import load_release

    rows: dict[str, dict[str, Any]] = {}
    for row in load_release(release, split=split).cases:
        task_id = case_key_for_row(row)
        if task_id in rows:
            raise EngineError(f"Duplicate task id {task_id!r} in {release}/{split}")
        rows[task_id] = row
    return rows


def task_label(row: dict[str, Any]) -> str:
    """Progress label: scenario/failure, size, and injection targets."""
    bits = [f"{row['scenario']}/{row['problem']}"]
    if row.get("topo_size"):
        bits.append(str(row["topo_size"]))
    inject = row.get("inject") or {}
    keys = [k for k in _INJECT_LABEL_KEYS if k in inject]
    keys += sorted(k for k in inject if k not in _INJECT_LABEL_KEYS)
    bits += [f"{_INJECT_KEY_SHORT.get(k, k)}={inject[k]}" for k in keys if inject[k]]
    return " ".join(bits)


def task_info(row: dict[str, Any]) -> TaskInfo:
    """What ``list_tasks`` reports about one task row."""
    return TaskInfo(
        task_id=case_key_for_row(row),
        scenario=str(row["scenario"]),
        topo_size=row.get("topo_size") or None,
        resource_class=CLASS_LARGE if row.get("topo_size") == "l" else CLASS_LIGHT,
        faults=[str(row["problem"])],
        label=task_label(row),
    )


def resolve_trial(request: TrialRequest) -> TrialRequest:
    """Return *request* with the row, failure, and ground truth it deploys.

    Mirrors the release runner's ``_run_trial``: one failure per row, its
    injection parameters, and the row's root causes unless they are marked
    unresolved.
    """
    if request.task_id is None:
        raise EngineError(
            "This benchmark engine starts release tasks by task_id; it does not "
            "accept task rows."
        )
    assert request.release is not None and request.split is not None
    row = release_task_rows(request.release, request.split).get(request.task_id)
    if row is None:
        raise EngineError(
            f"Unknown task id {request.task_id!r} in release {request.release} "
            f"split {request.split!r}."
        )
    return request.model_copy(
        update={
            "task": row,
            "failures": [row["problem"]],
            "inject": dict(row["inject"]),
            "expected_root_causes": (
                None
                if row.get("root_causes_status") == "unresolved"
                else row.get("root_causes")
            ),
        }
    )
