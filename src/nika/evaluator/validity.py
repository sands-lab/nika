"""Run-validity gates: flag trials whose result reflects a harness problem.

A low score can mean the agent was wrong, or that the run never gave the agent a
fair chance (the LLM output was cut off, the lab never started, the fault healed
before grading). This module detects the second kind from artifacts already in
the trial directory, so a run can be audited without re-executing anything.

Flags (a trial may carry several):

- ``no_submission``: no readable ``submission.json`` with a ``root_causes`` list.
- ``empty_report``: the frozen diagnosis report is empty, or only the agent's
  opening narration before its first tool call.
- ``empty_trace``: no model or tool interaction was recorded; always invalid.
- ``truncated_final_turn``: the last LLM turn of a phase stopped on the output
  token limit (``finish_reason == "length"`` / ``stop_reason == "max_tokens"``,
  or output tokens equal ``max_tokens`` with no text).
- ``max_steps_exhausted``: the diagnosis phase hit its step budget.
- ``fault_absent_at_cleanup``: the fault artifact was gone before cleanup, so
  the agent likely mutated the network or the fault self-healed.
- ``infra_error``: the trial failed to start or hit an infrastructure error.

``nika leaderboard validate`` always rejects empty trajectories and applies
the configured rate limit to ``HARNESS_FLAGS``; other flags produce warnings.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NO_SUBMISSION = "no_submission"
EMPTY_REPORT = "empty_report"
EMPTY_TRACE = "empty_trace"
TRUNCATED_FINAL_TURN = "truncated_final_turn"
MAX_STEPS_EXHAUSTED = "max_steps_exhausted"
FAULT_ABSENT_AT_CLEANUP = "fault_absent_at_cleanup"
INFRA_ERROR = "infra_error"

VALIDITY_FLAGS: tuple[str, ...] = (
    EMPTY_TRACE,
    NO_SUBMISSION,
    EMPTY_REPORT,
    TRUNCATED_FINAL_TURN,
    MAX_STEPS_EXHAUSTED,
    FAULT_ABSENT_AT_CLEANUP,
    INFRA_ERROR,
)

# Flags the harness (not the agent) is responsible for; these gate validation.
HARNESS_FLAGS: tuple[str, ...] = (TRUNCATED_FINAL_TURN, INFRA_ERROR)

DEFAULT_MAX_HARNESS_RATE = 0.05

_TOOL_CALL_FINISH_REASONS = frozenset({"tool_calls", "tool_use", "function_call"})
_TRUNCATION_FINISH_REASONS = frozenset({"length", "max_tokens"})
_SHORT_REPORT_CHARS = 200
_FAILED_STATUSES = frozenset({"error", "failed", "aborted", "interrupted"})


@dataclass
class TrialValidity:
    flags: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"validity_flags": list(self.flags), "validity_details": self.details}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    try:
        handle = path.open(encoding="utf-8")
    except OSError:
        return
    with handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _as_dict(value: Any) -> dict[str, Any]:
    """Trace rows may store nested payloads as dicts or as their ``repr``."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class _TraceFacts:
    has_activity: bool = False
    frozen_report: str | None = None
    max_steps: bool = False
    # phase -> (truncated, finish_reason) of the last LLM turn
    last_turn_truncated: dict[str, bool] = field(default_factory=dict)
    pre_tool_texts: set[str] = field(default_factory=set)


def _turn_truncated(row: dict[str, Any], max_tokens: int | None) -> bool:
    info = _as_dict(row.get("generation_info"))
    reason = str(info.get("finish_reason") or "").lower()
    if reason in _TRUNCATION_FINISH_REASONS:
        return True
    usage = _as_dict(row.get("usage_metadata"))
    out = _int(usage.get("output_tokens"))
    has_text = bool(str(row.get("text") or "").strip())
    return bool(max_tokens and out is not None and out >= max_tokens and not has_text)


def _has_agent_activity(row: dict[str, Any]) -> bool:
    event = row.get("event")
    return bool(
        event
        in (
            "llm_start",
            "llm_end",
            "llm_end_error",
            "tool_start",
            "tool_end",
            "tool_result",
            "tool_error",
            "item.completed",
            "result",
        )
        or (row.get("role") in ("assistant", "tool") and row.get("content"))
        or (
            event == "assistant"
            and _as_dict(_as_dict(row.get("claude_event")).get("message")).get(
                "content"
            )
        )
    )


def _read_trace(path: Path, max_tokens: int | None) -> _TraceFacts:
    facts = _TraceFacts()
    for row in _iter_jsonl(path):
        event = row.get("event")
        if _has_agent_activity(row):
            facts.has_activity = True
        phase = str(row.get("phase") or "")
        if event == "llm_end":
            facts.last_turn_truncated[phase] = _turn_truncated(row, max_tokens)
            reason = str(_as_dict(row.get("generation_info")).get("finish_reason"))
            text = str(row.get("text") or "").strip()
            if phase == "diagnosis" and text and reason in _TOOL_CALL_FINISH_REASONS:
                facts.pre_tool_texts.add(text)
        elif event == "result":
            # CLI agents report the stop reason once, on the final result event.
            stop = _as_dict(row.get("claude_event")).get("stop_reason")
            if str(stop or "").lower() in _TRUNCATION_FINISH_REASONS:
                facts.last_turn_truncated[phase] = True
        elif event == "max_steps_reached" and phase == "diagnosis":
            facts.max_steps = True
        elif event == "diagnosis_frozen":
            facts.frozen_report = str(row.get("report") or "")
    return facts


def has_agent_trace(path: Path) -> bool:
    """Require a recorded model or tool interaction, not just lifecycle rows."""
    return any(_has_agent_activity(row) for row in _iter_jsonl(path))


def _report_is_empty(report: str, pre_tool_texts: set[str]) -> bool:
    text = report.strip()
    if not text:
        return True
    if text in pre_tool_texts:
        return True
    return len(text) < _SHORT_REPORT_CHARS and "#" not in text


def _fault_absent_at_cleanup(path: Path) -> list[str]:
    absent: list[str] = []
    for row in _iter_jsonl(path):
        if row.get("event") != "fault_presence_recheck":
            continue
        data = _as_dict(row.get("data"))
        if data.get("phase") != "before_cleanup":
            continue
        if str(data.get("result") or data.get("read") or "").lower() == "absent":
            absent.append(str(data.get("fault") or "unknown"))
    return absent


def detect_trial_validity(
    session_dir: Path,
    *,
    run_meta: dict[str, Any] | None = None,
    score_status: str | None = None,
) -> TrialValidity:
    """Detect run-validity flags for one trial directory."""
    session_dir = Path(session_dir)
    meta = run_meta if run_meta is not None else _read_json(session_dir / "run.json")
    max_tokens = _int(meta.get("max_tokens"))
    result = TrialValidity()
    flags = result.flags

    submission = _read_json(session_dir / "submission.json")
    if not isinstance(submission.get("root_causes"), list):
        flags.append(NO_SUBMISSION)

    messages = session_dir / "messages.jsonl"
    trace = _read_trace(messages, max_tokens) if messages.is_file() else _TraceFacts()
    if not trace.has_activity:
        flags.append(EMPTY_TRACE)

    report = trace.frozen_report
    if report is None:
        report = submission.get("diagnosis_report")
    if not isinstance(report, str) or _report_is_empty(report, trace.pre_tool_texts):
        flags.append(EMPTY_REPORT)

    truncated = sorted(p for p, hit in trace.last_turn_truncated.items() if hit)
    if truncated:
        flags.append(TRUNCATED_FINAL_TURN)
        result.details["truncated_phases"] = truncated

    if trace.max_steps:
        flags.append(MAX_STEPS_EXHAUSTED)
        result.details["max_steps_wrapup_report"] = EMPTY_REPORT not in flags

    absent = _fault_absent_at_cleanup(session_dir / "nika.jsonl")
    if absent:
        flags.append(FAULT_ABSENT_AT_CLEANUP)
        result.details["absent_faults"] = absent

    status = score_status or meta.get("score_status")
    failed_start = (
        meta.get("status") in _FAILED_STATUSES
        or not (session_dir / "ground_truth.json").is_file()
    )
    if status == INFRA_ERROR or failed_start:
        flags.append(INFRA_ERROR)

    flags.sort(key=VALIDITY_FLAGS.index)
    return result


def count_validity_flags(flag_lists: Iterable[Iterable[str]]) -> dict[str, int]:
    """Per-flag trial counts, omitting flags that never fired."""
    counts: Counter[str] = Counter()
    for flags in flag_lists:
        counts.update(set(flags))
    return {flag: counts[flag] for flag in VALIDITY_FLAGS if counts[flag]}


def validity_rates(counts: dict[str, int], n_trials: int) -> dict[str, float]:
    denom = max(n_trials, 1)
    return {flag: count / denom for flag, count in counts.items()}


def validity_findings(
    counts: dict[str, int],
    n_trials: int,
    *,
    max_harness_rate: float = DEFAULT_MAX_HARNESS_RATE,
) -> tuple[list[str], list[str]]:
    """Return ``(errors, warnings)``: harness flags above the threshold fail."""
    errors: list[str] = []
    warnings: list[str] = []
    rates = validity_rates(counts, n_trials)
    for flag, count in counts.items():
        message = f"{count}/{n_trials} trials flagged {flag} ({rates[flag]:.1%})"
        if flag == EMPTY_TRACE:
            errors.append(message)
        elif flag in HARNESS_FLAGS and rates[flag] > max_harness_rate:
            errors.append(f"{message}; exceeds harness limit {max_harness_rate:.1%}")
        else:
            warnings.append(message)
    return errors, warnings
