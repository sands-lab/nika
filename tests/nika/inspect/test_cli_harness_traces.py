"""Inspect and trace parsers read OpenCode / DeepSeek Harness captures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent.cli.dsh.dsh_worker import DshWorker
from agent.cli.opencode.opencode_worker import OpenCodeWorker
from agent.protocols import DIAGNOSIS, SUBMISSION
from nika.evaluator.trace_parser import AgentTraceParser
from nika.inspect.adapters import load_agent_events, model_reply_count
from nika.inspect.catalog import list_raw_artifacts, session_content_hits
from nika.inspect.server import create_inspect_app
from nika.inspect.timeline import build_session_timeline

pytestmark = pytest.mark.unit

_CANONICAL = {
    "agent_start",
    "agent_done",
    "mcp_config",
    "prompt",
    "subprocess_start",
    "llm_start",
    "llm_end",
    "tool_start",
    "tool_end",
    "diagnosis_frozen",
}


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _events(path: Path) -> list[str]:
    return [
        json.loads(line)["event"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _feed_opencode(session_dir: Path) -> None:
    worker = OpenCodeWorker(
        session_id="sess",
        session_dir=str(session_dir),
        phase=DIAGNOSIS,
        llm_provider="openai",
        model="m1",
        stream_output=False,
    )
    worker._logger.log("agent_start", {"agent": "cli.opencode"})
    worker._logger.log("mcp_config", {"phase": DIAGNOSIS, "servers": ["kathara_base"]})
    worker._logger.log("prompt", {"text": "Diagnose the incident."})
    worker._logger.log("subprocess_start", {"command": "opencode run ..."})
    worker._handle_stdout_line(
        json.dumps({"type": "step_start", "part": {"messageID": "msg_1"}})
    )
    worker._handle_stdout_line(
        json.dumps(
            {
                "type": "tool_use",
                "part": {
                    "tool": "show_bgp_summary",
                    "callID": "call_1",
                    "state": {
                        "status": "running",
                        "input": {"device": "r1"},
                    },
                },
            }
        )
    )
    worker._handle_stdout_line(
        json.dumps(
            {
                "type": "tool_use",
                "part": {
                    "tool": "show_bgp_summary",
                    "callID": "call_1",
                    "state": {
                        "status": "completed",
                        "input": {"device": "r1"},
                        "output": "neighbor down",
                    },
                },
            }
        )
    )
    worker._handle_stdout_line(
        json.dumps({"type": "text", "part": {"text": "BGP neighbor is down."}})
    )
    worker._handle_stdout_line(
        json.dumps(
            {
                "type": "step_finish",
                "part": {"tokens": {"input": 12, "output": 6, "reasoning": 0}},
            }
        )
    )
    worker._logger.log("agent_done", {})
    worker._logger.log("diagnosis_frozen", {"report": "BGP neighbor is down."})
    worker._logger.log("agent_start", {"phase": SUBMISSION})
    worker._logger.log("agent_done", {})


def _feed_dsh(session_dir: Path) -> None:
    worker = DshWorker(
        session_id="sess",
        session_dir=str(session_dir),
        phase=DIAGNOSIS,
        model="deepseek-v4-flash",
        llm_provider="deepseek",
        stream_output=False,
    )
    worker._logger.log("agent_start", {"agent": "cli.dsh"})
    worker._logger.log("mcp_config", {"phase": DIAGNOSIS, "servers": ["kathara_base"]})
    worker._logger.log("prompt", {"text": "Diagnose the incident."})
    worker._logger.log("subprocess_start", {"command": "dsh --profile headless ..."})
    worker._log_dsh_event({"type": "status", "phase": "turn_start", "turn": 1})
    worker._log_dsh_event(
        {"type": "status", "phase": "step_start", "turn": 1, "step": 1}
    )
    worker._log_dsh_event(
        {
            "type": "tool_call",
            "callId": "c1",
            "tool": "show_bgp_summary",
            "input": {"device": "r1"},
        }
    )
    worker._log_dsh_event(
        {
            "type": "tool_result",
            "callId": "c1",
            "tool": "show_bgp_summary",
            "status": "ok",
            "result": "neighbor down",
        }
    )
    worker._log_dsh_event({"type": "thinking", "text": "interface looks down"})
    worker._log_dsh_event({"type": "text", "text": "BGP neighbor is down."})
    worker._log_dsh_event(
        {
            "type": "status",
            "phase": "step_end",
            "turn": 1,
            "step": 1,
            "usage": {"input_tokens": 12, "output_tokens": 6},
        }
    )
    worker._log_dsh_event({"type": "status", "phase": "turn_end", "turn": 1})
    worker._log_dsh_event({"type": "final", "text": "BGP neighbor is down."})
    worker._logger.log("agent_done", {})
    worker._logger.log("diagnosis_frozen", {"report": "BGP neighbor is down."})
    worker._logger.log("agent_start", {"phase": SUBMISSION})
    worker._logger.log("agent_done", {})


def _make_session(root: Path, session_id: str, agent_type: str, feed) -> Path:
    session_dir = root / session_id
    session_dir.mkdir()
    _write_json(
        session_dir / "run.json",
        {
            "session_id": session_id,
            "status": "finished",
            "scenario_name": "dc_clos",
            "scenario_topo_size": "s",
            "agent_type": agent_type,
            "model": "m1",
            "start_time": "2026-01-01T12:00:00",
            "end_time": "2026-01-01T12:05:00",
            "problem_names": ["link_down"],
            "outcome": "success",
        },
    )
    _write_jsonl(
        session_dir / "nika.jsonl",
        [
            {
                "timestamp": "2026-01-01T12:01:00",
                "event": "env_start",
                "message": "Lab deployed",
            },
            {
                "timestamp": "2026-01-01T12:01:05",
                "event": "failure_injected",
                "message": "Injected link_down",
            },
            {
                "timestamp": "2026-01-01T12:01:06",
                "event": "agent_start",
                "message": "Agent started",
            },
            {
                "timestamp": "2026-01-01T12:02:00",
                "event": "agent_end",
                "message": "Agent finished",
            },
        ],
    )
    _write_json(
        session_dir / "submission.json",
        {"is_anomaly": True, "root_causes": [{"type": "link_down"}]},
    )
    feed(session_dir)
    return session_dir


@pytest.mark.parametrize(
    ("agent_type", "feed"),
    [
        ("cli.opencode", _feed_opencode),
        ("cli.dsh", _feed_dsh),
    ],
)
def test_harness_messages_match_canonical_contract(
    tmp_path: Path, agent_type: str, feed
) -> None:
    session_dir = _make_session(tmp_path, "sess-trace", agent_type, feed)
    events = _events(session_dir / "messages.jsonl")
    assert set(events) <= _CANONICAL
    assert events.count("llm_start") == 1
    assert events.count("llm_end") == 1
    assert events.count("tool_start") == 1
    assert events.count("tool_end") == 1
    native = {
        "step_start",
        "step_finish",
        "tool_use",
        "text",
        "thinking",
        "status",
        "final",
    }
    assert native.isdisjoint(events)

    parsed = AgentTraceParser(str(session_dir / "messages.jsonl")).parse_trace()
    assert parsed["steps"] == 1
    assert parsed["tool_calls"] == 1
    assert parsed["tool_errors"] == 0
    assert parsed["in_tokens"] == 12
    assert parsed["out_tokens"] == 6

    adapted = load_agent_events(session_dir)
    kinds = [ev.kind for ev in adapted]
    assert kinds.count("llm") == 2  # llm_start + llm_end
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert "phase" in kinds
    assert "submission" in kinds
    tool = next(ev for ev in adapted if ev.kind == "tool_call")
    assert tool.tool is not None
    assert tool.tool.name == "show_bgp_summary"
    reply = next(ev for ev in adapted if ev.event == "llm_end")
    assert "BGP neighbor is down." in (reply.summary or "")
    assert model_reply_count(adapted, phase=DIAGNOSIS) == 1

    timeline = build_session_timeline(session_dir)
    titles = [ev.event for ev in timeline]
    assert titles.index("failure_injected") < titles.index("tool_start")
    assert "env_start" in titles
    assert session_content_hits(session_dir, "show_bgp_summary")
    names = {item.name for item in list_raw_artifacts(session_dir)}
    assert "messages.jsonl" in names
    assert "nika.jsonl" in names
    assert "run.json" in names


def test_inspect_api_reads_harness_sessions(tmp_path: Path) -> None:
    root = tmp_path / "results"
    root.mkdir()
    _make_session(root, "opencode-1", "cli.opencode", _feed_opencode)
    _make_session(root, "dsh-1", "cli.dsh", _feed_dsh)
    client = TestClient(create_inspect_app(results_root=root))

    listed = client.get("/api/sessions")
    assert listed.status_code == 200
    ids = {row["session_id"] for row in listed.json()["sessions"]}
    assert ids == {"opencode-1", "dsh-1"}

    for sid, agent_type in (("opencode-1", "cli.opencode"), ("dsh-1", "cli.dsh")):
        detail = client.get(f"/api/sessions/{sid}")
        assert detail.status_code == 200
        assert detail.json()["agent_type"] == agent_type

        timeline = client.get(f"/api/sessions/{sid}/timeline")
        assert timeline.status_code == 200
        events = timeline.json()["events"]
        kinds = {ev["kind"] for ev in events}
        names = {ev["event"] for ev in events}
        assert {"lifecycle", "llm", "tool_call", "tool_result"} <= kinds
        assert {"env_start", "llm_start", "llm_end", "tool_start", "tool_end"} <= names
        assert "step_start" not in names
        assert "tool_use" not in names

        messages = client.get(f"/api/sessions/{sid}/messages")
        assert messages.status_code == 200
        agent_events = messages.json()["events"]
        assert all(ev["source"] == "agent" for ev in agent_events)
        assert any(ev["event"] == "llm_end" for ev in agent_events)

        search = client.get(
            f"/api/sessions/{sid}/search", params={"q": "neighbor down"}
        )
        assert search.status_code == 200
        assert search.json()["event_ids"]

        files = client.get(f"/api/sessions/{sid}/files")
        assert files.status_code == 200
        raw_names = {item["name"] for item in files.json()["files"]}
        assert "messages.jsonl" in raw_names

        raw = client.get(f"/api/sessions/{sid}/raw/messages.jsonl")
        assert raw.status_code == 200
        rows = raw.json()["data"]
        assert any(row.get("event") == "llm_end" for row in rows)
        assert any("show_bgp_summary" in json.dumps(row) for row in rows)
