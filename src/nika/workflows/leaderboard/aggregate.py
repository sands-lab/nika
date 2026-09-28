"""Aggregate per-trial leaderboard metrics."""

from __future__ import annotations

from typing import Any

from collections import Counter

from nika.evaluator.score_status import SCORE_STATUSES, ScoreStatus
from nika.workflows.leaderboard.schema import (
    PRIMARY_METRIC,
    SCORE_METRIC_KEYS,
    AggregatedMetrics,
    RcaConfusion,
    RcaConfusionPair,
    TrialResult,
)

_FLOAT_TOL = 1e-9
_HEALTHY_PROBLEM = "healthy"


def score_for_primary(value: float | int | None, *, score_status: ScoreStatus) -> float:
    """Map a trial score into the primary mean: non-scored → 0.0; negatives excluded."""
    if score_status != "scored":
        return 0.0
    if value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number < 0:
        return 0.0
    return number


def score_for_average(value: float | int | None, *, outcome: str) -> float:
    """Backward-compatible shim; prefer ``score_for_primary`` with ``score_status``."""
    status: ScoreStatus = "scored" if outcome == "success" else "no_submission"
    return score_for_primary(value, score_status=status)


def extract_trial_metrics(raw: dict[str, Any]) -> dict[str, float | int | None]:
    metrics: dict[str, float | int | None] = {}
    for key in SCORE_METRIC_KEYS:
        if key in raw:
            metrics[key] = raw[key]
    for key in ("in_tokens", "out_tokens", "steps", "tool_calls", "tool_errors"):
        if key in raw:
            metrics[key] = raw[key]
    return metrics


def _is_healthy_trial(trial: TrialResult) -> bool:
    return trial.problem == _HEALTHY_PROBLEM or not str(trial.problem or "").strip()


def _empty_status_counts() -> dict[str, int]:
    return {status: 0 for status in SCORE_STATUSES}


def aggregate_trial_results(
    trials: list[TrialResult],
    *,
    n_trials_expected: int,
) -> AggregatedMetrics:
    n_present = len(trials)
    n_success = sum(1 for t in trials if t.outcome == "success")
    n_failed = sum(1 for t in trials if t.outcome == "agent_failed")
    denom = max(n_trials_expected, 1)

    status_counts = _empty_status_counts()
    for trial in trials:
        status_counts[trial.score_status] = status_counts.get(trial.score_status, 0) + 1

    n_scored = status_counts.get("scored", 0)
    submission_rate = n_scored / denom

    def primary_mean(key: str) -> float:
        total = 0.0
        for trial in trials:
            total += score_for_primary(
                trial.metrics.get(key), score_status=trial.score_status
            )
        return total / denom

    def conditional_mean(key: str) -> float | None:
        if n_scored == 0:
            return None
        total = 0.0
        for trial in trials:
            if trial.score_status != "scored":
                continue
            total += score_for_primary(
                trial.metrics.get(key), score_status=trial.score_status
            )
        return total / n_scored

    fault_trials = [t for t in trials if not _is_healthy_trial(t)]
    healthy_trials = [t for t in trials if _is_healthy_trial(t)]

    def slice_primary_mean(slice_trials: list[TrialResult], key: str) -> float:
        slice_denom = max(len(slice_trials), 1) if slice_trials else 1
        if not slice_trials:
            return 0.0
        total = sum(
            score_for_primary(t.metrics.get(key), score_status=t.score_status)
            for t in slice_trials
        )
        return total / slice_denom

    in_tokens = 0
    out_tokens = 0
    steps = 0
    tool_calls = 0
    tool_errors = 0
    for trial in trials:
        in_tokens += int(trial.metrics.get("in_tokens") or 0)
        out_tokens += int(trial.metrics.get("out_tokens") or 0)
        steps += int(trial.metrics.get("steps") or 0)
        tool_calls += int(trial.metrics.get("tool_calls") or 0)
        tool_errors += int(trial.metrics.get("tool_errors") or 0)

    return AggregatedMetrics(
        primary_metric=PRIMARY_METRIC,
        mean_rca_f1=primary_mean("rca_f1"),
        mean_localization_f1=primary_mean("localization_f1"),
        mean_detection_score=primary_mean("detection_score"),
        n_trials_expected=n_trials_expected,
        n_trials_present=n_present,
        n_success=n_success,
        n_agent_failed=n_failed,
        status_counts=status_counts,
        submission_rate=submission_rate,
        conditional_mean_rca_f1=conditional_mean("rca_f1"),
        conditional_mean_localization_f1=conditional_mean("localization_f1"),
        conditional_mean_detection_score=conditional_mean("detection_score"),
        fault_mean_rca_f1=slice_primary_mean(fault_trials, "rca_f1"),
        fault_mean_localization_f1=slice_primary_mean(fault_trials, "localization_f1"),
        healthy_mean_detection_score=slice_primary_mean(
            healthy_trials, "detection_score"
        ),
        token_totals={"in_tokens": in_tokens, "out_tokens": out_tokens},
        steps_totals={
            "steps": steps,
            "tool_calls": tool_calls,
            "tool_errors": tool_errors,
        },
    )


def metrics_nearly_equal(a: AggregatedMetrics, b: AggregatedMetrics) -> list[str]:
    """Return mismatch descriptions if aggregates disagree."""
    issues: list[str] = []
    for field in (
        "primary_metric",
        "n_trials_expected",
        "n_trials_present",
        "n_success",
        "n_agent_failed",
        "status_counts",
    ):
        if getattr(a, field) != getattr(b, field):
            issues.append(
                f"metrics.{field}: expected {getattr(a, field)!r}, got {getattr(b, field)!r}"
            )
    for field in (
        "mean_rca_f1",
        "mean_localization_f1",
        "mean_detection_score",
        "submission_rate",
        "fault_mean_rca_f1",
        "fault_mean_localization_f1",
        "healthy_mean_detection_score",
    ):
        left = float(getattr(a, field))
        right = float(getattr(b, field))
        if abs(left - right) > _FLOAT_TOL:
            issues.append(f"metrics.{field}: expected {left}, got {right}")
    for field in (
        "conditional_mean_rca_f1",
        "conditional_mean_localization_f1",
        "conditional_mean_detection_score",
    ):
        left = getattr(a, field)
        right = getattr(b, field)
        if left is None and right is None:
            continue
        if left is None or right is None:
            issues.append(f"metrics.{field}: expected {left!r}, got {right!r}")
            continue
        if abs(float(left) - float(right)) > _FLOAT_TOL:
            issues.append(f"metrics.{field}: expected {left}, got {right}")
    if a.token_totals != b.token_totals:
        issues.append(
            f"metrics.token_totals: expected {a.token_totals}, got {b.token_totals}"
        )
    if a.steps_totals != b.steps_totals:
        issues.append(
            f"metrics.steps_totals: expected {a.steps_totals}, got {b.steps_totals}"
        )
    return issues


def build_rca_confusion(trials: list[TrialResult]) -> RcaConfusion:
    """Build multi-label GT→predicted edge counts for RCA confusion display."""
    pair_counts: Counter[tuple[str, str]] = Counter()
    missing_ids: list[str] = []
    for trial in trials:
        if trial.predicted_fault_types is None:
            missing_ids.append(trial.trial_id)
            continue
        for gt in trial.gt_fault_types:
            for pred in trial.predicted_fault_types:
                pair_counts[(gt, pred)] += 1
    pairs = [
        RcaConfusionPair(gt=gt, predicted=pred, count=count)
        for (gt, pred), count in sorted(
            pair_counts.items(), key=lambda item: (-item[1], item[0][0], item[0][1])
        )
    ]
    return RcaConfusion(
        pairs=pairs,
        n_missing_prediction=len(missing_ids),
        missing_prediction_trial_ids=sorted(missing_ids),
    )
