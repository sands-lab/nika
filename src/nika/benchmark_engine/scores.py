"""Grade a trial for the ``score`` operation with this release's scorer.

The values are those of :func:`nika.workflows.eval.session.generic_eval`, the
scorer release 0.1.0 used. Only the envelope follows engine API version 1:
a score status, and no ``-1.0`` placeholders, which 0.1.0's leaderboard
already counted as 0. The frontend writes ``eval_metrics.json``; this module
reads ``submission.json`` and ``ground_truth.json`` and writes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from nika.benchmark_engine.protocol import Scores

SCORE_KEYS = (
    "detection_score",
    "localization_accuracy",
    "localization_precision",
    "localization_recall",
    "localization_f1",
    "rca_accuracy",
    "rca_precision",
    "rca_recall",
    "rca_f1",
    "fault_type_precision",
    "fault_type_recall",
    "fault_type_f1",
)


def _read_json(path: Path) -> tuple[Any, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{path.name} unreadable: {exc}"


def _metrics(value: float | None) -> dict[str, float | None]:
    return dict.fromkeys(SCORE_KEYS, value)


def grade_trial(
    trial_dir: str | Path, *, scoring_id: str, infra_evidence: bool = False
) -> Scores:
    """Grade the submission in *trial_dir* against its ground truth.

    ``no_submission`` scores zeros. ``infra_error`` (no submission after an
    infrastructure failure, or no ground truth) and ``grading_error`` (an
    unreadable submission or ground truth, or a scorer exception) score nulls.
    """
    from nika.workflows.eval.session import generic_eval

    trial_dir = Path(trial_dir)
    gt, gt_error = _read_json(trial_dir / "ground_truth.json")
    if gt_error is None and not isinstance(gt, dict):
        gt_error = "ground_truth.json is not an object"

    submission_path = trial_dir / "submission.json"
    submission: Any = None
    submission_error: str | None = None
    if submission_path.exists():
        submission, submission_error = _read_json(submission_path)

    if submission is None and submission_error is None:
        if infra_evidence or gt_error:
            return Scores(
                status="infra_error",
                scoring_id=scoring_id,
                metrics=_metrics(None),
                error=gt_error or "infrastructure failure before the agent submitted",
            )
        return Scores(
            status="no_submission", scoring_id=scoring_id, metrics=_metrics(0.0)
        )
    if gt_error or submission_error:
        return Scores(
            status="grading_error",
            scoring_id=scoring_id,
            metrics=_metrics(None),
            error=gt_error or submission_error,
        )
    try:
        values = generic_eval(gt, submission)
    except Exception as exc:  # noqa: BLE001 - reported as the trial's grade
        return Scores(
            status="grading_error",
            scoring_id=scoring_id,
            metrics=_metrics(None),
            error=f"{type(exc).__name__}: {exc}",
        )
    # 0.1.0 marked an answer it could not parse with -1.0 and counted it as 0.
    metrics = {key: max(0.0, float(values.get(key, 0.0))) for key in SCORE_KEYS}
    return Scores(status="scored", scoring_id=scoring_id, metrics=metrics)
