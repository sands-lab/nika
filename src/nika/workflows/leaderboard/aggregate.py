"""Aggregate per-trial leaderboard metrics."""

from __future__ import annotations

from typing import Any

from nika.workflows.leaderboard.schema import (
    PRIMARY_METRIC,
    SCORE_METRIC_KEYS,
    AggregatedMetrics,
    TrialResult,
)

_FLOAT_TOL = 1e-9


def score_for_average(value: float | int | None, *, outcome: str) -> float:
    """Map a trial score into the mean: agent_failed / invalid → 0.0."""
    if outcome != "success":
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


def extract_trial_metrics(raw: dict[str, Any]) -> dict[str, float | int | None]:
    metrics: dict[str, float | int | None] = {}
    for key in SCORE_METRIC_KEYS:
        if key in raw:
            metrics[key] = raw[key]
    for key in ("in_tokens", "out_tokens", "steps", "tool_calls", "tool_errors"):
        if key in raw:
            metrics[key] = raw[key]
    return metrics


def aggregate_trial_results(
    trials: list[TrialResult],
    *,
    n_trials_expected: int,
) -> AggregatedMetrics:
    n_present = len(trials)
    n_success = sum(1 for t in trials if t.outcome == "success")
    n_failed = sum(1 for t in trials if t.outcome == "agent_failed")
    denom = max(n_trials_expected, 1)

    def mean_of(key: str) -> float:
        total = 0.0
        for trial in trials:
            total += score_for_average(trial.metrics.get(key), outcome=trial.outcome)
        # Missing expected trials count as 0.
        return total / denom

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
        mean_rca_f1=mean_of("rca_f1"),
        mean_localization_f1=mean_of("localization_f1"),
        mean_detection_score=mean_of("detection_score"),
        n_trials_expected=n_trials_expected,
        n_trials_present=n_present,
        n_success=n_success,
        n_agent_failed=n_failed,
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
    ):
        if getattr(a, field) != getattr(b, field):
            issues.append(
                f"metrics.{field}: expected {getattr(a, field)!r}, got {getattr(b, field)!r}"
            )
    for field in ("mean_rca_f1", "mean_localization_f1", "mean_detection_score"):
        left = float(getattr(a, field))
        right = float(getattr(b, field))
        if abs(left - right) > _FLOAT_TOL:
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
