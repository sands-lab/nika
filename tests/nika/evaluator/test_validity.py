"""Run-validity gates on synthetic trial directories (formats from real runs)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika.evaluator.validity import (
    DEFAULT_MAX_HARNESS_RATE,
    detect_trial_validity,
    validity_findings,
)

pytestmark = pytest.mark.unit

LONG_REPORT = "# Report\n\n" + "The OSPF adjacency on r1 is down. " * 20


def _jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _llm_end(phase: str, *, reason: str, text: str = "", out: int = 10) -> dict:
    return {
        "phase": phase,
        "event": "llm_end",
        "text": text,
        "generation_info": str({"finish_reason": reason, "model_name": "m"}),
        "usage_metadata": str({"input_tokens": 5, "output_tokens": out}),
    }


def _trial(
    root: Path,
    *,
    submission: bool = True,
    report: str = LONG_REPORT,
    turns: list[dict] | None = None,
    extra_events: list[dict] | None = None,
    recheck: list[str] | None = None,
    ground_truth: bool = True,
) -> Path:
    root.mkdir(parents=True)
    (root / "run.json").write_text(
        json.dumps({"status": "finished", "max_tokens": 100})
    )
    if ground_truth:
        (root / "ground_truth.json").write_text("{}")
    if submission:
        (root / "submission.json").write_text(json.dumps({"root_causes": []}))
    rows = list(turns or [_llm_end("diagnosis", reason="stop", text=report)])
    rows += extra_events or []
    rows.append({"phase": "diagnosis", "event": "diagnosis_frozen", "report": report})
    _jsonl(root / "messages.jsonl", rows)
    _jsonl(
        root / "nika.jsonl",
        [
            {
                "event": "fault_presence_recheck",
                "data": {"phase": "before_cleanup", "fault": f, "result": "absent"},
            }
            for f in recheck or []
        ],
    )
    return root


def test_clean_trial_has_no_flags(tmp_path: Path) -> None:
    assert detect_trial_validity(_trial(tmp_path / "t")).flags == []


def test_no_submission_and_infra(tmp_path: Path) -> None:
    flags = detect_trial_validity(
        _trial(tmp_path / "t", submission=False, ground_truth=False)
    ).flags
    assert flags == ["no_submission", "infra_error"]


def test_truncated_submission_turn_and_token_cap(tmp_path: Path) -> None:
    turns = [
        _llm_end("diagnosis", reason="stop", text=LONG_REPORT),
        _llm_end("submission", reason="length", out=100),
    ]
    result = detect_trial_validity(_trial(tmp_path / "a", turns=turns))
    assert result.flags == ["truncated_final_turn"]
    assert result.details["truncated_phases"] == ["submission"]

    capped = [_llm_end("diagnosis", reason="stop", out=100)]
    assert (
        "truncated_final_turn"
        in detect_trial_validity(_trial(tmp_path / "b", turns=capped)).flags
    )


def test_max_steps_with_pre_tool_narration_is_empty_report(tmp_path: Path) -> None:
    narration = "Let me check connectivity first."
    turns = [_llm_end("diagnosis", reason="tool_calls", text=narration)]
    events = [{"phase": "diagnosis", "event": "max_steps_reached", "max_steps": 100}]
    result = detect_trial_validity(
        _trial(tmp_path / "t", report=narration, turns=turns, extra_events=events)
    )
    assert result.flags == ["empty_report", "max_steps_exhausted"]
    assert result.details["max_steps_wrapup_report"] is False


def test_max_steps_with_wrapup_report(tmp_path: Path) -> None:
    events = [{"phase": "diagnosis", "event": "max_steps_reached", "max_steps": 100}]
    result = detect_trial_validity(_trial(tmp_path / "t", extra_events=events))
    assert result.flags == ["max_steps_exhausted"]
    assert result.details["max_steps_wrapup_report"] is True


def test_fault_absent_at_cleanup(tmp_path: Path) -> None:
    result = detect_trial_validity(_trial(tmp_path / "t", recheck=["dhcp_down"]))
    assert result.flags == ["fault_absent_at_cleanup"]


def test_findings_fail_only_on_harness_flags_over_threshold() -> None:
    counts = {"truncated_final_turn": 6, "max_steps_exhausted": 50}
    errors, warnings = validity_findings(counts, 100)
    assert len(errors) == 1 and "truncated_final_turn" in errors[0]
    assert len(warnings) == 1 and "max_steps_exhausted" in warnings[0]

    errors, warnings = validity_findings({"infra_error": 5}, 100)
    assert not errors and warnings  # at the 5% limit, not above it
    assert DEFAULT_MAX_HARNESS_RATE == 0.05
