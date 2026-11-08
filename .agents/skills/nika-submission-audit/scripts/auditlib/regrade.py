"""Re-grade a trial from ``submission.json`` and ``ground_truth.json``.

``nika leaderboard validate`` recomputes aggregates from per-trial results but
trusts the per-trial scores. The audit re-scores each trial with the current
scoring and names the cause of any difference: a known scoring revision, or
nothing known (``unexplained``), which is a grading lead. It also flags F1
values that do not follow from the submitted precision and recall
(``inconsistent_metrics``), a sign the result was edited by hand.
"""

from __future__ import annotations

from typing import Any

from auditlib.trace import Trial
from nika.evaluator.scoring import (
    GradingError,
    _parse_is_anomaly,
    parse_root_causes,
    resource_within,
    score_detection,
    score_rca_v2,
)

COMPARED = ("rca_f1", "localization_f1", "fault_type_f1", "detection_score")
_TOLERANCE = 1e-6


def _cause(trial: Trial) -> str:
    sub, gt = trial.submission, trial.ground_truth
    preds = parse_root_causes(sub) or []
    truths = parse_root_causes(gt) or []
    if (
        not truths
        and not preds
        and _parse_is_anomaly(sub.get("is_anomaly")) is not False
    ):
        return "healthy_credit"
    pred_ids = {p.resource.id for p in preds}
    truth_ids = {t.resource.id for t in truths}
    if pred_ids != truth_ids and any(
        p.resource.id not in truth_ids and resource_within(p.resource, t.resource)
        for p in preds
        for t in truths
    ):
        return "containment"
    return "unexplained"


def _inconsistent(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Submitted F1 values that do not follow from the submitted P and R."""
    rows = []
    for prefix in ("rca", "localization", "fault_type"):
        p, r, f1 = (metrics.get(f"{prefix}_{k}") for k in ("precision", "recall", "f1"))
        if not all(isinstance(v, (int, float)) for v in (p, r, f1)):
            continue
        expected = 2 * p * r / (p + r) if p + r else 0.0
        if abs(expected - f1) > 1e-3:
            rows.append(
                {
                    "metric": f"{prefix}_f1",
                    "submitted": round(float(f1), 4),
                    "regraded": round(expected, 4),
                    "cause": "inconsistent_metrics",
                }
            )
    return rows


def regrade_trial(trial: Trial) -> list[dict[str, Any]]:
    """Metrics whose submitted value differs from a re-grade.

    Returns one row per differing metric with ``cause``. Trials without a
    submission or recorded metrics are skipped; validity flags cover them.
    """
    if not trial.submission or not trial.eval_metrics or not trial.ground_truth:
        return []
    try:
        regraded: dict[str, float] = dict(
            score_rca_v2(trial.submission, trial.ground_truth)
        )
        regraded["detection_score"] = score_detection(
            trial.submission, trial.ground_truth
        )
    except (GradingError, ValueError, TypeError) as exc:
        return [
            {
                "metric": "grading",
                "submitted": None,
                "regraded": None,
                "cause": "grading_error",
                "error": str(exc),
            }
        ]
    diffs: list[dict[str, Any]] = _inconsistent(trial.eval_metrics)
    cause: str | None = None
    for metric in COMPARED:
        submitted = trial.eval_metrics.get(metric)
        if not isinstance(submitted, (int, float)) or metric not in regraded:
            continue
        if abs(float(submitted) - regraded[metric]) > _TOLERANCE:
            cause = cause or _cause(trial)
            diffs.append(
                {
                    "metric": metric,
                    "submitted": round(float(submitted), 4),
                    "regraded": round(regraded[metric], 4),
                    "cause": cause,
                }
            )
    return diffs
