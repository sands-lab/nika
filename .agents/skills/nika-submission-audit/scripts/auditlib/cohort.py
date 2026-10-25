"""Submission-level metrics: patterns no single trial shows.

A healthy scenario that most trials call faulty, a case no configuration
solves, a case solved with almost no tool calls, a label agents always confuse
with another, or a tool that keeps failing are benchmark or harness leads,
not agent mistakes. ``metric`` signatures in the knowledge base name one of
:data:`METRICS` with a threshold; :func:`run_metrics` reports each group that
crosses it.

Earlier submissions to the same release (``history``) widen the case-level
metrics from one configuration to every configuration seen so far.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from auditlib.kb import Issue, KnowledgeBase, Signature

_QUICK_SOLVE_CALLS = 3
_SAMPLE = 8


@dataclass
class TrialRecord:
    """What the metrics need from one trial (current or historical)."""

    trial_id: str
    source: str
    case_key: str
    scenario: str
    gt_types: list[str]
    pred_types: list[str]
    pred_anomaly: bool | None
    rca_f1: float | None
    tool_calls: int | None = None
    mutating: bool = False
    tools: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return not self.gt_types


@dataclass
class MetricHit:
    issue: str
    metric: str
    group: str
    value: float
    n: int
    trials: list[str]
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in ({}, None)}


Groups = dict[str, list[TrialRecord]]
Metric = Callable[
    [Signature, list[TrialRecord], list[TrialRecord]],
    list[tuple[str, float, int, list[str], dict[str, Any]]],
]


def _group(records: Iterable[TrialRecord], key: Callable[[TrialRecord], str]) -> Groups:
    groups: Groups = defaultdict(list)
    for rec in records:
        groups[key(rec)].append(rec)
    return groups


def _rows(
    groups: Groups,
    value: Callable[
        [list[TrialRecord]], tuple[float, list[TrialRecord], dict[str, Any]]
    ],
    sig: Signature,
):
    out = []
    for name, recs in sorted(groups.items()):
        if len(recs) < sig.min_trials:
            continue
        val, support, detail = value(recs)
        if sig.threshold is not None and val >= sig.threshold:
            out.append(
                (
                    name,
                    round(val, 3),
                    len(recs),
                    [r.trial_id for r in support][:_SAMPLE],
                    detail,
                )
            )
    return out


def _flags_anomaly(rec: TrialRecord) -> bool:
    return bool(rec.pred_types) or rec.pred_anomaly is True


def healthy_fp_rate(sig, current, history):
    groups = _group((r for r in current if r.healthy), lambda r: r.scenario)

    def value(recs):
        bad = [r for r in recs if _flags_anomaly(r)]
        return len(bad) / len(recs), bad, {}

    return _rows(groups, value, sig)


def healthy_label_share(sig, current, history):
    groups = _group((r for r in current if r.healthy), lambda r: r.scenario)

    def value(recs):
        counts = Counter(t for r in recs for t in set(r.pred_types))
        if not counts:
            return 0.0, [], {}
        label, n = counts.most_common(1)[0]
        source = current[0].source
        support = [r for r in recs if label in r.pred_types and r.source == source]
        return n / len(recs), support, {"label": label}

    return _rows(groups, value, sig)


def _scored(records):
    return [r for r in records if r.rca_f1 is not None]


def _case_fault_types(recs: list[TrialRecord]) -> list[str]:
    # Case keys truncate fault type names, so scope matches the full names.
    return sorted({t for r in recs for t in r.gt_types})


def case_fail_rate(sig, current, history):
    """Share of trials scoring 0, over this submission and every earlier one."""
    pool = _scored(current) + _scored(history)
    cases = {r.case_key for r in current if not r.healthy}
    groups = _group((r for r in pool if r.case_key in cases), lambda r: r.case_key)

    def value(recs):
        zero = [r for r in recs if (r.rca_f1 or 0.0) == 0.0]
        sources = len({r.source for r in recs})
        return (
            len(zero) / len(recs),
            # Earlier submissions count toward the rate, but only this
            # submission's trials can be opened as evidence.
            [r for r in zero if r.source == current[0].source],
            {"configurations": sources, "fault_types": _case_fault_types(recs)},
        )

    return _rows(groups, value, sig)


def case_quick_solve_share(sig, current, history):
    groups = _group(
        (r for r in _scored(current) if not r.healthy), lambda r: r.case_key
    )

    def value(recs):
        quick = [
            r
            for r in recs
            if r.rca_f1 == 1.0
            and r.tool_calls is not None
            and r.tool_calls <= _QUICK_SOLVE_CALLS
        ]
        return (
            len(quick) / len(recs),
            quick,
            {
                "max_tool_calls": _QUICK_SOLVE_CALLS,
                "fault_types": _case_fault_types(recs),
            },
        )

    return _rows(groups, value, sig)


def label_confusion(sig, current, history):
    """Share of a fault type's trials that name one specific other label."""
    pool = _scored(current) + _scored(history)
    groups = _group((r for r in pool if len(r.gt_types) == 1), lambda r: r.gt_types[0])

    def value(recs):
        wrong = Counter(
            t
            for r in recs
            if r.gt_types[0] not in r.pred_types
            for t in set(r.pred_types)
        )
        if not wrong:
            return 0.0, [], {}
        label, n = wrong.most_common(1)[0]
        source = current[0].source
        support = [r for r in recs if label in r.pred_types and r.source == source]
        return n / len(recs), support, {"confused_with": label}

    return _rows(groups, value, sig)


def _tool_rate(key: str):
    def metric(sig, current, history):
        totals: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, key: 0})
        support: dict[str, list[TrialRecord]] = defaultdict(list)
        for rec in current:
            for tool, row in rec.tools.items():
                totals[tool]["calls"] += row.get("calls", 0)
                totals[tool][key] += row.get(key, 0)
                if row.get(key):
                    support[tool].append(rec)
        out = []
        for tool, row in sorted(totals.items()):
            if row["calls"] < max(sig.min_trials, 1):
                continue
            rate = row[key] / row["calls"]
            if sig.threshold is not None and rate >= sig.threshold:
                out.append(
                    (
                        tool,
                        round(rate, 3),
                        row["calls"],
                        [r.trial_id for r in support[tool]][:_SAMPLE],
                        {key: row[key]},
                    )
                )
        return out

    return metric


def trial_error_rate(sig, current, history):
    """Share of one trial's tool results that are errors: a burst of harness
    failures inside a single trial, which submission-wide rates average away.
    """
    out = []
    for rec in current:
        calls = sum(row.get("calls", 0) for row in rec.tools.values())
        errors = sum(row.get("errors", 0) for row in rec.tools.values())
        if calls < max(sig.min_trials, 1):
            continue
        rate = errors / calls
        if sig.threshold is not None and rate >= sig.threshold:
            out.append(
                (
                    rec.trial_id,
                    round(rate, 3),
                    calls,
                    [rec.trial_id],
                    {"errors": errors},
                )
            )
    return out


def mutation_rate(sig, current, history):
    groups = {"submission": list(current)}

    def value(recs):
        bad = [r for r in recs if r.mutating]
        return len(bad) / len(recs), bad, {}

    return _rows(groups, value, sig)


METRICS: dict[str, Metric] = {
    "healthy_fp_rate": healthy_fp_rate,
    "healthy_label_share": healthy_label_share,
    "case_fail_rate": case_fail_rate,
    "case_quick_solve_share": case_quick_solve_share,
    "label_confusion": label_confusion,
    "tool_error_rate": _tool_rate("errors"),
    "tool_truncation_rate": _tool_rate("truncated"),
    "trial_error_rate": trial_error_rate,
    "mutation_rate": mutation_rate,
}


def run_metrics(
    kb: KnowledgeBase, current: list[TrialRecord], history: list[TrialRecord]
) -> list[MetricHit]:
    hits: list[MetricHit] = []
    if not current:
        return hits
    for issue in kb.issues:
        for sig in issue.detect:
            if sig.kind != "metric":
                continue
            metric = METRICS.get(str(sig.metric))
            if metric is None:
                raise ValueError(f"{issue.id}: unknown metric {sig.metric!r}")
            for group, value, n, trials, detail in metric(sig, current, history):
                if not _issue_matches_group(issue, sig, group, detail):
                    continue
                hits.append(
                    MetricHit(
                        issue.id, str(sig.metric), group, value, n, trials, detail
                    )
                )
    return hits


def _issue_matches_group(
    issue: Issue, sig: Signature, group: str, detail: dict[str, Any]
) -> bool:
    """Known issues narrow a metric to their scope; leads take every group."""
    scope = issue.scope
    if sig.metric in ("healthy_fp_rate", "healthy_label_share") and scope.scenarios:
        if group not in scope.scenarios:
            return False
    if sig.metric in ("case_fail_rate", "case_quick_solve_share") and scope.fault_types:
        # Scope may name the full fault type or a part of the case key.
        full = set(detail.get("fault_types", []))
        if not any(ft in full or ft in group for ft in scope.fault_types):
            return False
    if (
        sig.metric == "label_confusion"
        and scope.fault_types
        and group not in scope.fault_types
    ):
        return False
    if sig.regex is not None:
        target = " ".join([group, *(str(v) for v in detail.values())])
        return bool(sig.regex.search(target))
    return True


def history_from_scores(
    root: Path, *, release: str, exclude: str | None = None
) -> list[TrialRecord]:
    """Trial records from leaderboard scores packages (``submissions/<release>/*``)."""
    base = Path(root) / "submissions" / release
    if not base.is_dir():
        base = Path(root)
    records: list[TrialRecord] = []
    for result in sorted(base.glob("*/results/trials/*/result.json")):
        slug = result.parents[3].name
        if slug == exclude:
            continue
        try:
            data = json.loads(result.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        metrics = data.get("metrics") or {}
        records.append(
            TrialRecord(
                trial_id=str(data.get("trial_id")),
                source=slug,
                case_key=str(data.get("case_key")),
                scenario=str(data.get("scenario")),
                gt_types=list(data.get("gt_fault_types") or []),
                pred_types=list(data.get("predicted_fault_types") or []),
                pred_anomaly=None,
                rca_f1=metrics.get("rca_f1"),
                tool_calls=metrics.get("tool_calls"),
            )
        )
    return records
