"""Shared two-phase contract: bookends, ERROR policy, and max-steps report."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.utils.loggers import MessageLogger
from agent.utils.two_phase import TwoPhaseAgent, max_steps_report

pytestmark = pytest.mark.unit


class _Agent(TwoPhaseAgent):
    def __init__(self, trace_dir: Path, report: str) -> None:
        self.session_id = "sess"
        self.trace_dir = str(trace_dir)
        self.stream_output = False
        self.report = report
        self.submitted = False

    async def diagnose(self, task_description: str) -> str:
        return self.report

    async def submit(self, diagnosis_report: str, context: dict) -> str:
        self.submitted = True
        return "ok"


def _events(trace_dir: Path) -> list[tuple[str, str]]:
    rows = (trace_dir / "messages.jsonl").read_text().splitlines()
    return [(json.loads(r)["phase"], json.loads(r)["event"]) for r in rows]


def test_error_report_fails_before_submission(tmp_path: Path, monkeypatch) -> None:
    advanced: list[str] = []
    monkeypatch.setattr(
        "agent.utils.mcp_client.begin_submission_mcp_phase",
        lambda sid, report: advanced.append(report) or {},
    )
    agent = _Agent(tmp_path, "ERROR: diagnosis phase exited with code 1")
    with pytest.raises(RuntimeError, match="exited with code 1"):
        asyncio.run(agent.run("task"))
    assert not advanced and not agent.submitted
    assert _events(tmp_path) == [
        ("diagnosis", "agent_start"),
        ("diagnosis", "agent_error"),
    ]


def test_run_freezes_report_then_submits(tmp_path: Path, monkeypatch) -> None:
    advanced: list[str] = []
    monkeypatch.setattr(
        "agent.utils.mcp_client.begin_submission_mcp_phase",
        lambda sid, report: advanced.append(report) or {"resources": []},
    )
    agent = _Agent(tmp_path, "pc1 eth0 down")
    result = asyncio.run(agent.run("task"))
    assert advanced == ["pc1 eth0 down"]
    assert result == {"diagnosis_report": "pc1 eth0 down", "submission_result": "ok"}
    assert _events(tmp_path) == [
        ("diagnosis", "agent_start"),
        ("diagnosis", "agent_done"),
        ("submission", "agent_start"),
        ("submission", "agent_done"),
    ]


def test_max_steps_report_keeps_latest_text_or_fails(tmp_path: Path) -> None:
    logger = MessageLogger(phase="diagnosis", session_dir=str(tmp_path))
    assert max_steps_report(logger, max_steps=3, latest_text=" bgp down ") == "bgp down"
    assert max_steps_report(logger, max_steps=3, latest_text="").startswith("ERROR:")
    assert _events(tmp_path) == [("diagnosis", "max_steps_reached")] * 2
