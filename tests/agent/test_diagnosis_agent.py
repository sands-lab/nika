"""DiagnosisAgent contract: bookends, ERROR policy, submission modes, phase runner."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.base import DiagnosisAgent, max_steps_report
from agent.runtime.__main__ import main as run_phase
from agent.utils.loggers import MessageLogger
from nika.agents.registry import run_pipeline

pytestmark = pytest.mark.unit


class _Agent(DiagnosisAgent):
    def __init__(self, trace_dir: Path, report: str) -> None:
        self.session_id = "sess"
        self.scenario_name = "scenario"
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


@pytest.mark.parametrize(
    ("report", "match"),
    [
        ("ERROR: diagnosis phase exited with code 1", "exited with code 1"),
        ("  \n", "empty report"),
    ],
)
def test_error_report_fails_before_submission(
    tmp_path: Path, monkeypatch, report: str, match: str
) -> None:
    advanced: list[str] = []
    monkeypatch.setattr(
        "nika.agents.mcp_config.begin_submission_mcp_phase",
        lambda sid, report: advanced.append(report) or {},
    )
    agent = _Agent(tmp_path, report)
    with pytest.raises(RuntimeError, match=match):
        asyncio.run(run_pipeline(agent, "task"))
    assert not advanced and not agent.submitted
    assert _events(tmp_path) == [
        ("diagnosis", "agent_start"),
        ("diagnosis", "agent_error"),
    ]


def test_run_freezes_report_then_submits(tmp_path: Path, monkeypatch) -> None:
    advanced: list[str] = []
    monkeypatch.setattr(
        "nika.agents.mcp_config.begin_submission_mcp_phase",
        lambda sid, report: advanced.append(report) or {"resources": []},
    )
    agent = _Agent(tmp_path, "pc1 eth0 down")
    asyncio.run(run_pipeline(agent, "task"))
    assert advanced == ["pc1 eth0 down"] and agent.submitted
    assert _events(tmp_path) == [
        ("diagnosis", "agent_start"),
        ("diagnosis", "agent_done"),
        ("submission", "agent_start"),
        ("submission", "agent_done"),
    ]


class _Mapper:
    async def submit(self, report: str, context: dict, **_: str) -> str:
        return f"Submitted: {report}"

    def trace_fields(self) -> dict:
        return {"submission_mode": "unstructured", "model": "m"}


def test_report_mapper_replaces_agent_submit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "nika.agents.mcp_config.begin_submission_mcp_phase",
        lambda sid, report: {"resources": []},
    )
    agent = _Agent(tmp_path, "pc1 eth0 down")
    asyncio.run(run_pipeline(agent, "task", report_mapper=_Mapper()))
    assert not agent.submitted
    rows = [
        json.loads(r) for r in (tmp_path / "messages.jsonl").read_text().splitlines()
    ]
    start = next(r for r in rows if r["phase"] == "submission")
    assert (
        start["event"] == "agent_start" and start["submission_mode"] == "unstructured"
    )


def test_max_steps_report_keeps_latest_text_or_fails(tmp_path: Path) -> None:
    logger = MessageLogger(phase="diagnosis", session_dir=str(tmp_path))
    assert max_steps_report(logger, max_steps=3, latest_text=" bgp down ") == "bgp down"
    assert max_steps_report(logger, max_steps=3, latest_text="").startswith("ERROR:")
    assert _events(tmp_path) == [("diagnosis", "max_steps_reached")] * 2


class _RunnerAgent(DiagnosisAgent):
    """Agent the sandbox runner builds from ``task.json``."""

    async def diagnose(self, task_description: str) -> str:
        return f"{self.model} saw {task_description}"

    async def submit(self, diagnosis_report: str, context: dict) -> str:
        return f"submitted {diagnosis_report} with {len(context['resources'])}"


def test_runner_runs_one_phase_per_process(agent_task) -> None:
    """Diagnosis leaves report.json; submission reads it and the host context."""
    workspace = agent_task(
        agent_class=f"{__name__}:_RunnerAgent",
        model="m1",
        llm_provider="custom",
        task_description="bgp down",
        stream_output=False,
    )
    run_phase("diagnosis")
    report = json.loads((workspace / "report.json").read_text())["report"]
    assert report == "m1 saw bgp down"

    (workspace / "submission_context.json").write_text(
        json.dumps({"resources": [{"id": "node/r1"}]})
    )
    run_phase("submission")
    events = _events(workspace)
    assert events == [
        ("diagnosis", "agent_start"),
        ("diagnosis", "agent_done"),
        ("submission", "agent_start"),
        ("submission", "agent_done"),
    ]
