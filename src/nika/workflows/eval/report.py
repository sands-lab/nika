"""Aggregate finished session artifacts into a renderer-agnostic summary report.

Scoring reuses ``nika.workflows.leaderboard.aggregate`` so this report and a
leaderboard submission built from the same result directory cannot disagree.
Two things the raw summary CSV cannot express are handled here:

* ``outcome=agent_failed`` trials carry ``-1.0`` metric sentinels. Averaging
  those raw yields negative scores; ``score_for_average`` maps them to 0.0.
* Healthy (no-fault) cases have no root cause, so RCA/localization are
  meaningless for them and only ``detection_score`` is reported.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from nika.utils.session_artifacts import RUN_FILENAME
from nika.workflows.leaderboard.aggregate import (
    aggregate_trial_results,
    build_rca_confusion,
    score_for_average,
)
from nika.workflows.leaderboard.schema import PRIMARY_METRIC, TrialResult
from nika.workflows.leaderboard.trial_results import (
    TrialResultError,
    trial_result_from_dir,
)

# Metric families the report can rank breakdown rows by.
REPORT_METRICS = ("rca_f1", "localization_f1", "detection_score")

# Breakdown dimensions, in default display order.
GROUP_DIMENSIONS = ("domain", "env", "problem", "size")

_GROUP_LABELS = {
    "domain": "failure domain",
    "env": "scenario / net env",
    "problem": "problem (root cause)",
    "size": "topology size",
}

_UNKNOWN = "<unknown>"

# Display label for no-fault control cases, mirroring
# ``nika.workflows.benchmark.healthy.HEALTHY_PROBLEM``. That module is not
# imported here because its package eagerly loads the whole benchmark runner,
# which reporting has no reason to depend on. A session is healthy when
# ``problem_names`` is empty or solely this label.
_HEALTHY_LABEL = "healthy"


def _is_healthy_names(names: list[str]) -> bool:
    return not names or names == [_HEALTHY_LABEL]


@dataclass
class MetricStats:
    """Clamped means for one slice of trials."""

    n_trials: int = 0
    n_agent_failed: int = 0
    rca_f1: float = 0.0
    localization_f1: float = 0.0
    detection_score: float = 0.0

    def value(self, metric: str) -> float:
        return float(getattr(self, metric))


@dataclass
class GroupRow:
    key: str
    stats: MetricStats


@dataclass
class Breakdown:
    dimension: str
    label: str
    rows: list[GroupRow] = field(default_factory=list)


@dataclass
class ConfusionRow:
    gt: str
    predicted: str
    count: int


@dataclass
class SummaryReport:
    """Everything a renderer needs; no formatting decisions baked in."""

    result_dir: str
    primary_metric: str = PRIMARY_METRIC
    agent_types: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)

    # Run health.
    n_trials_present: int = 0
    n_trials_expected: int = 0
    n_success: int = 0
    n_agent_failed: int = 0
    n_unreadable: int = 0
    expected_is_known: bool = False

    # Headline means over every trial, missing expected trials counted as 0.
    mean_rca_f1: float = 0.0
    mean_localization_f1: float = 0.0
    mean_detection_score: float = 0.0

    # Fault vs. healthy (no-fault control) split, over present trials.
    fault_stats: MetricStats = field(default_factory=MetricStats)
    healthy_stats: MetricStats = field(default_factory=MetricStats)

    breakdowns: list[Breakdown] = field(default_factory=list)
    confusion: list[ConfusionRow] = field(default_factory=list)
    n_missing_prediction: int = 0

    # Cost.
    token_totals: dict[str, int] = field(default_factory=dict)
    steps_totals: dict[str, int] = field(default_factory=dict)

    # Metric families that were entirely absent (e.g. llm_judge_* with no judge run).
    empty_metric_families: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _TrialRecord:
    """One trial plus the report-only dimensions the leaderboard does not carry."""

    result: TrialResult
    is_healthy: bool
    failure_domain: str
    topo_size: str
    agent_type: str
    model: str


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _problem_names(run_meta: dict[str, Any]) -> list[str]:
    """Injected fault ids for a session; empty / ``healthy`` means a control case."""
    names = run_meta.get("problem_names")
    if isinstance(names, list):
        return [str(name) for name in names if str(name).strip()]
    if isinstance(names, str) and names.strip():
        return [names.strip()]
    problem = run_meta.get("problem")
    if problem and str(problem).strip():
        return [str(problem).strip()]
    return []


def _primary_problem(names: list[str]) -> str:
    if _is_healthy_names(names):
        return _HEALTHY_LABEL
    if len(names) > 1:
        return "+".join(names)
    return names[0]


def _failure_domain(session_dir: Path, run_meta: dict[str, Any]) -> str:
    """Prefer ground truth, then run metadata, then the problem taxonomy."""
    gt = _read_json(session_dir / "ground_truth.json")
    domain = str(gt.get("failure_domain") or "").strip()
    if domain:
        return domain
    domain = str(run_meta.get("failure_domain") or "").strip()
    if domain:
        return domain
    from nika.evaluator.result_log import resolve_failure_metadata

    return str(resolve_failure_metadata(run_meta).get("failure_domain") or _UNKNOWN)


def _stats_for(records: list[_TrialRecord]) -> MetricStats:
    """Clamped means over the given records; denominator is the record count."""
    denom = max(len(records), 1)
    stats = MetricStats(
        n_trials=len(records),
        n_agent_failed=sum(1 for r in records if r.result.outcome != "success"),
    )
    for metric in REPORT_METRICS:
        total = sum(
            score_for_average(r.result.metrics.get(metric), outcome=r.result.outcome)
            for r in records
        )
        setattr(stats, metric, total / denom)
    return stats


def _breakdown(
    records: list[_TrialRecord],
    dimension: str,
    *,
    metric: str,
) -> Breakdown:
    keyed: dict[str, list[_TrialRecord]] = {}
    for record in records:
        if dimension == "domain":
            key = record.failure_domain
        elif dimension == "env":
            key = record.result.scenario
        elif dimension == "problem":
            key = record.result.problem
        else:
            key = record.topo_size
        keyed.setdefault(key or _UNKNOWN, []).append(record)

    rows = [GroupRow(key=key, stats=_stats_for(group)) for key, group in keyed.items()]
    # Worst-performing last so a truncated view still shows the strong slices.
    rows.sort(key=lambda row: (-row.stats.value(metric), row.key))
    return Breakdown(
        dimension=dimension,
        label=_GROUP_LABELS.get(dimension, dimension),
        rows=rows,
    )


def _empty_metric_families(session_dirs: list[Path]) -> list[str]:
    """Report which optional metric families produced no data at all."""
    from nika.evaluator.result_log import LLM_JUDGE_FILENAME

    families: list[str] = []
    if not any((d / LLM_JUDGE_FILENAME).is_file() for d in session_dirs):
        families.append("llm_judge_*")
    return families


def _expected_trial_count(result_dir: Path) -> int | None:
    """Expected trial count from the run config at the result-dir root.

    Prefers ``planned_trial_count`` (scoped ``--task-id`` runs), else
    ``case_count * n_trials``.
    """
    for name in (RUN_FILENAME, "benchmark_job.json"):
        run_cfg = _read_json(result_dir / name)
        planned = run_cfg.get("planned_trial_count")
        if isinstance(planned, int) and planned > 0:
            return planned
        case_count = run_cfg.get("case_count")
        n_trials = run_cfg.get("n_trials")
        if (
            isinstance(case_count, int)
            and isinstance(n_trials, int)
            and case_count > 0
            and n_trials > 0
        ):
            return case_count * n_trials
    return None


def build_summary_report(
    session_dirs: list[Path],
    *,
    result_dir: str | Path,
    metric: str = PRIMARY_METRIC,
    dimensions: tuple[str, ...] | list[str] = GROUP_DIMENSIONS,
    n_trials_expected: int | None = None,
    include_confusion: bool = True,
) -> SummaryReport:
    """Aggregate ``session_dirs`` into a ``SummaryReport``.

    ``n_trials_expected`` sets the headline denominator so trials that never
    landed count as 0, matching leaderboard scoring. When ``None`` it is read
    from the result-dir run config; callers that filtered the session set must
    pass the present count instead, or the denominator will be wrong.
    """
    if metric not in REPORT_METRICS:
        raise ValueError(
            f"Unknown report metric {metric!r}; expected one of {', '.join(REPORT_METRICS)}"
        )
    unknown_dims = [d for d in dimensions if d not in GROUP_DIMENSIONS]
    if unknown_dims:
        raise ValueError(
            f"Unknown group dimension(s) {', '.join(unknown_dims)}; "
            f"expected from {', '.join(GROUP_DIMENSIONS)}"
        )

    root = Path(result_dir)
    records: list[_TrialRecord] = []
    n_unreadable = 0

    for session_dir in session_dirs:
        run_meta = _read_json(session_dir / RUN_FILENAME)
        problem_names = _problem_names(run_meta)
        problem = _primary_problem(problem_names)
        try:
            result = trial_result_from_dir(
                trial_id=str(run_meta.get("session_id") or session_dir.name),
                case_key=session_dir.name,
                trial_index=1,
                scenario=str(run_meta.get("scenario_name") or _UNKNOWN),
                problem=problem,
                session_dir=session_dir,
            )
        except (TrialResultError, json.JSONDecodeError, OSError):
            # A session without a usable outcome cannot be scored either way;
            # count it so the run-health line stays honest.
            n_unreadable += 1
            continue

        records.append(
            _TrialRecord(
                result=result,
                is_healthy=_is_healthy_names(problem_names),
                failure_domain=_failure_domain(session_dir, run_meta),
                topo_size=str(run_meta.get("scenario_topo_size") or "") or _UNKNOWN,
                agent_type=str(run_meta.get("agent_type") or ""),
                model=str(run_meta.get("model") or ""),
            )
        )

    if n_trials_expected is None:
        n_trials_expected = _expected_trial_count(root)
    expected_is_known = n_trials_expected is not None
    expected = max(int(n_trials_expected or 0), len(records))

    trial_results = [r.result for r in records]
    aggregated = aggregate_trial_results(trial_results, n_trials_expected=expected)

    fault_records = [r for r in records if not r.is_healthy]
    healthy_records = [r for r in records if r.is_healthy]

    report = SummaryReport(
        result_dir=str(root),
        agent_types=sorted({r.agent_type for r in records if r.agent_type}),
        models=sorted({r.model for r in records if r.model}),
        n_trials_present=len(records),
        n_trials_expected=expected,
        n_success=aggregated.n_success,
        n_agent_failed=aggregated.n_agent_failed,
        n_unreadable=n_unreadable,
        expected_is_known=expected_is_known,
        mean_rca_f1=aggregated.mean_rca_f1,
        mean_localization_f1=aggregated.mean_localization_f1,
        mean_detection_score=aggregated.mean_detection_score,
        fault_stats=_stats_for(fault_records),
        healthy_stats=_stats_for(healthy_records),
        token_totals=dict(aggregated.token_totals),
        steps_totals=dict(aggregated.steps_totals),
        empty_metric_families=_empty_metric_families(session_dirs),
    )

    # Breakdowns cover fault cases only: RCA/localization are undefined for
    # healthy controls, which get their own headline row instead.
    report.breakdowns = [
        _breakdown(fault_records, dimension, metric=metric)
        for dimension in dimensions
        if fault_records
    ]

    if include_confusion and fault_records:
        confusion = build_rca_confusion([r.result for r in fault_records])
        report.confusion = [
            ConfusionRow(gt=pair.gt, predicted=pair.predicted, count=pair.count)
            for pair in confusion.pairs
        ]
        report.n_missing_prediction = confusion.n_missing_prediction

    return report
