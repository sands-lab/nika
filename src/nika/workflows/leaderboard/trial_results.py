"""Build ``TrialResult`` records from finished session/trial directories.

Shared by leaderboard packing (release-driven, every expected trial must be
present) and offline eval reporting (discovery-driven, scans whatever landed).
Both paths must derive outcome, fault types, and metrics identically so their
aggregates cannot disagree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nika.utils.session_artifacts import RUN_FILENAME
from nika.workflows.leaderboard.aggregate import extract_trial_metrics
from nika.workflows.leaderboard.schema import TrialResult

GROUND_TRUTH_FILENAME = "ground_truth.json"
SUBMISSION_FILENAME = "submission.json"
EVAL_METRICS_FILENAME = "eval_metrics.json"

VALID_OUTCOMES = frozenset({"success", "agent_failed"})


class TrialResultError(ValueError):
    """A trial directory cannot be read as a scored trial result."""


def read_json_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TrialResultError(f"Expected JSON object at {path}")
    return data


def _read_json_object_or_none(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return read_json_object(path)
    except (TrialResultError, json.JSONDecodeError, OSError):
        return None


def fault_types_from_root_causes(raw: Any) -> list[str] | None:
    """Unique fault_type values from a root_causes list, preserving order."""
    if not isinstance(raw, list):
        return None
    names: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        fault_type = str(item.get("fault_type") or "").strip()
        if fault_type and fault_type not in names:
            names.append(fault_type)
    return names or None


def gt_fault_types(session_dir: Path, *, problem: str) -> list[str]:
    gt = _read_json_object_or_none(session_dir / GROUND_TRUTH_FILENAME)
    if gt is not None:
        names = fault_types_from_root_causes(gt.get("root_causes"))
        if names:
            return names
    return [problem]


def predicted_fault_types(session_dir: Path) -> list[str] | None:
    submission = _read_json_object_or_none(session_dir / SUBMISSION_FILENAME)
    if submission is None:
        return None
    return fault_types_from_root_causes(submission.get("root_causes"))


def trial_result_from_dir(
    *,
    trial_id: str,
    case_key: str,
    trial_index: int,
    scenario: str,
    problem: str,
    session_dir: Path,
) -> TrialResult:
    """Read one trial directory into a ``TrialResult``.

    Raises ``TrialResultError`` when ``run.json`` carries no usable outcome.
    """
    run_meta = read_json_object(session_dir / RUN_FILENAME)
    outcome = str(run_meta.get("outcome") or "")
    if outcome not in VALID_OUTCOMES:
        raise TrialResultError(
            f"Trial {trial_id} has invalid outcome {outcome!r} under {session_dir}"
        )
    metrics_path = session_dir / EVAL_METRICS_FILENAME
    metrics = (
        extract_trial_metrics(read_json_object(metrics_path))
        if metrics_path.is_file()
        else {}
    )

    return TrialResult(
        trial_id=trial_id,
        case_key=case_key,
        trial_index=trial_index,
        scenario=scenario,
        problem=problem,
        outcome=outcome,  # type: ignore[arg-type]
        metrics=metrics,
        gt_fault_types=gt_fault_types(session_dir, problem=problem),
        predicted_fault_types=predicted_fault_types(session_dir),
    )
