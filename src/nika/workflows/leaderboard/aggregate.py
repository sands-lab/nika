"""Aggregate per-trial leaderboard metrics."""

from __future__ import annotations

from typing import Any

from collections import Counter, defaultdict
from math import isfinite, sqrt
from statistics import mean

from scipy.stats import t as student_t

from nika.workflows.benchmark.network_identity import (
    MAIN_SLICES,
    DATASET_METADATA_FIELDS,
)
from nika.workflows.benchmark.trials import task_id_for_row

from nika.evaluator.validity import count_validity_flags
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
    task_rows: list[dict[str, Any]] | None = None,
    trials_per_task: int | None = None,
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

    case_solved: dict[str, bool] = {}
    for trial in trials:
        solved = trial.score_status == "scored" and trial.metrics.get("rca_f1") == 1
        case_solved[trial.case_key] = case_solved.get(trial.case_key, True) and solved
    case_consistency = (
        sum(case_solved.values()) / len(case_solved) if case_solved else 0.0
    )

    def unasked(trial: TrialResult, key: str) -> bool:
        # A scored trial whose task does not ask for this objective has no score.
        return (
            key != "rca_f1"
            and trial.score_status == "scored"
            and trial.metrics.get(key) is None
        )

    def primary_mean(key: str) -> float:
        total = 0.0
        for trial in trials:
            total += score_for_primary(
                trial.metrics.get(key), score_status=trial.score_status
            )
        return total / max(denom - sum(unasked(t, key) for t in trials), 1)

    def conditional_mean(key: str) -> float | None:
        asked = [
            t for t in trials if t.score_status == "scored" and not unasked(t, key)
        ]
        if not asked:
            return None
        total = sum(
            score_for_primary(t.metrics.get(key), score_status=t.score_status)
            for t in asked
        )
        return total / len(asked)

    fault_trials = [t for t in trials if not _is_healthy_trial(t)]
    healthy_trials = [t for t in trials if _is_healthy_trial(t)]

    def slice_primary_mean(slice_trials: list[TrialResult], key: str) -> float:
        slice_trials = [t for t in slice_trials if not unasked(t, key)]
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

    result = AggregatedMetrics(
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
        case_consistency=case_consistency,
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
        validity_counts=count_validity_flags(t.validity_flags for t in trials),
    )

    if task_rows is not None or any(t.dataset for t in trials):
        _aggregate_release_tasks(
            result, trials, task_rows=task_rows, trials_per_task=trials_per_task
        )
    return result


def _aggregate_release_tasks(result, trials, *, task_rows, trials_per_task):
    """Average trials within tasks before averaging tasks and matched deltas."""
    groups = defaultdict(list)
    metadata = {}
    problems = {}
    objectives = {}
    if task_rows is not None:
        for row in task_rows:
            key = task_id_for_row(row)
            groups[key] = []
            problems[key] = row["problem"]
            objectives[key] = row.get(
                "objectives", ["detection", "localization", "rca"]
            )
            metadata[key] = {k: row[k] for k in DATASET_METADATA_FIELDS if k in row}
            metadata[key].setdefault(
                "slice",
                "healthy"
                if row["problem"] == "healthy"
                else "multi_fault"
                if "+" in row["problem"]
                else "core",
            )
    for trial in trials:
        if task_rows is not None and trial.case_key not in metadata:
            raise ValueError(
                f"Trial references an unknown release task: {trial.case_key}"
            )
        groups[trial.case_key].append(trial)
        problems[trial.case_key] = trial.problem
        if task_rows is None:
            metadata[trial.case_key] = trial.dataset

    def task_mean(key, metric):
        objective = (
            "detection"
            if metric == "detection_score"
            else "localization"
            if metric == "localization_f1"
            else "rca"
        )
        if key in objectives and objective not in objectives[key]:
            return None
        asked = [
            t
            for t in groups[key]
            if key in objectives
            or metric == "rca_f1"
            or not (t.score_status == "scored" and t.metrics.get(metric) is None)
        ]
        missing = max(0, (trials_per_task or len(groups[key])) - len(groups[key]))
        denominator = len(asked) + missing
        if not denominator:
            return None
        return (
            sum(
                score_for_primary(t.metrics.get(metric), score_status=t.score_status)
                for t in asked
            )
            / denominator
        )

    def average(keys, metric):
        values = [v for key in keys if (v := task_mean(key, metric)) is not None]
        return mean(values) if values else None

    main = [k for k, m in metadata.items() if m.get("slice", "core") in MAIN_SLICES]
    result.main_score = average(main, "rca_f1")
    result.mean_rca_f1 = result.main_score or 0.0
    result.mean_localization_f1 = average(main, "localization_f1") or 0.0
    result.mean_detection_score = average(main, "detection_score") or 0.0
    for metric in ("rca_f1", "localization_f1", "detection_score"):
        values = []
        for key in main:
            objective = {
                "rca_f1": "rca",
                "localization_f1": "localization",
                "detection_score": "detection",
            }[metric]
            if key in objectives and objective not in objectives[key]:
                continue
            scored = [
                t
                for t in groups[key]
                if t.score_status == "scored"
                and (
                    key in objectives
                    or metric == "rca_f1"
                    or t.metrics.get(metric) is not None
                )
            ]
            if scored:
                values.append(
                    mean(
                        score_for_primary(
                            t.metrics.get(metric), score_status=t.score_status
                        )
                        for t in scored
                    )
                )
        setattr(result, f"conditional_mean_{metric}", mean(values) if values else None)
    result.case_consistency = (
        mean(
            bool(groups[key])
            and (trials_per_task is None or len(groups[key]) == trials_per_task)
            and all(
                t.score_status == "scored" and t.metrics.get("rca_f1") == 1
                for t in groups[key]
            )
            for key in main
        )
        if main
        else 0.0
    )
    healthy = [k for k in main if metadata[k].get("slice") == "healthy"]
    # FPR is defined on submitted healthy diagnoses; submission coverage is
    # reported separately. Missing submissions still receive zero main credit.
    rates = []
    for key in healthy:
        scored = [
            t
            for t in groups[key]
            if t.score_status == "scored"
            and t.metrics.get("detection_score") is not None
        ]
        if scored:
            rates.append(mean(1 - float(t.metrics["detection_score"]) for t in scored))
    result.healthy_false_positive_rate = mean(rates) if rates else None
    result.healthy_mean_detection_score = average(healthy, "detection_score") or 0.0
    result.multi_fault_mean_rca_f1 = average(
        [k for k in main if metadata[k].get("slice") == "multi_fault"], "rca_f1"
    )
    faults = [k for k in main if metadata[k].get("slice", "core") != "healthy"]
    result.fault_mean_rca_f1 = average(faults, "rca_f1") or 0.0
    result.fault_mean_localization_f1 = average(faults, "localization_f1") or 0.0

    comparisons = defaultdict(list)
    unmatched = Counter()
    for key, m in sorted(metadata.items()):
        if m.get("slice") != "vendor_twin":
            continue
        vendor = m["router_platform"]
        tier = m.get("vendor_tier")
        if tier not in {"A", "C"}:
            raise ValueError("Vendor twins require vendor_tier: A or C")
        targets = [(tier, vendor)]
        if tier == "A":
            targets.append((tier, "all_vendors"))
        base = m.get("base_task_id")
        if (
            base not in metadata
            or metadata[base].get("slice") != "core"
            or not groups[key]
            or not groups[base]
        ):
            for target in targets:
                unmatched[target] += 1
            continue
        twin_value, base_value = task_mean(key, "rca_f1"), task_mean(base, "rca_f1")
        if twin_value is None or base_value is None:
            for target in targets:
                unmatched[target] += 1
            continue
        fault = problems[key]
        for target in targets:
            comparisons[target].append((fault, twin_value - base_value))
    for tier, vendor in sorted(comparisons.keys() | unmatched.keys()):
        pairs = comparisons[tier, vendor]
        delta = mean(d for _, d in pairs) if pairs else None
        clusters = defaultdict(float)
        for fault, d in pairs:
            clusters[fault] += d - delta
        g, n = len(clusters), len(pairs)
        interval = None
        if g > 1:
            se = sqrt(g / (g - 1) * sum(v * v for v in clusters.values())) / n
            radius = float(student_t.ppf(0.975, g - 1)) * se
            interval = [delta - radius, delta + radius]
        result.vendor_deltas.setdefault(tier, {})[vendor] = {
            "delta": delta,
            "ci95": interval,
            "n_pairs": n,
            "n_fault_clusters": g,
            "n_unmatched": unmatched[tier, vendor],
            "ci_method": "fault-clustered t interval",
        }
    size_groups = defaultdict(list)
    for key, m in metadata.items():
        if not m.get("family") or not m.get("topo_scale"):
            continue
        if m.get("slice") not in {"core", "third_size"} or m.get(
            "size_comparison_excluded"
        ):
            continue
        kind = "isp_graph_classes" if m.get("family") == "isp" else "generator_families"
        size_groups[kind, m.get("family"), m.get("topo_scale")].append(key)
    for (kind, family, scale), keys in sorted(size_groups.items()):
        result.size_slices.setdefault(kind, {}).setdefault(family, {})[scale] = {
            "mean_rca_f1": average(keys, "rca_f1"),
            "n_tasks": len(keys),
        }


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
        "validity_counts",
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
        if not (isfinite(left) and isfinite(right)) or abs(left - right) > _FLOAT_TOL:
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
        if not (isfinite(left) and isfinite(right)) or abs(left - right) > _FLOAT_TOL:
            issues.append(f"metrics.{field}: expected {left}, got {right}")
    if a.token_totals != b.token_totals:
        issues.append(
            f"metrics.token_totals: expected {a.token_totals}, got {b.token_totals}"
        )
    if a.steps_totals != b.steps_totals:
        issues.append(
            f"metrics.steps_totals: expected {a.steps_totals}, got {b.steps_totals}"
        )
    for field in (
        "main_score",
        "healthy_false_positive_rate",
        "multi_fault_mean_rca_f1",
        "vendor_deltas",
        "size_slices",
    ):
        if getattr(a, field) != getattr(b, field):
            issues.append(
                f"metrics.{field}: expected {getattr(a, field)!r}, got {getattr(b, field)!r}"
            )
    # Older packages omit this additive metric.
    if a.case_consistency is not None and b.case_consistency is not None:
        if (
            not (isfinite(a.case_consistency) and isfinite(b.case_consistency))
            or abs(a.case_consistency - b.case_consistency) > _FLOAT_TOL
        ):
            issues.append(
                f"metrics.case_consistency: expected {a.case_consistency}, "
                f"got {b.case_consistency}"
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
