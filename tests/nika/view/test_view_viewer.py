"""Unit tests for the session viewer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from nika.view.adapters import adapt_agent_event, adapt_nika_event
from nika.view.catalog import list_sessions, summarize_session_dir
from nika.view.models import CanonicalTraceEvent
from nika.view.server import create_view_app
from nika.view.timeline import merge_timelines


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def fixture_root(tmp_path: Path) -> Path:
    finished = tmp_path / "20260101-120000-abc123"
    finished.mkdir()
    _write_json(
        finished / "run.json",
        {
            "session_id": "20260101-120000-abc123",
            "status": "finished",
            "scenario_name": "dc_clos",
            "scenario_topo_size": "s",
            "agent_type": "byo.langgraph",
            "model": "gpt-test",
            "start_time": "2026-01-01T12:00:00",
            "end_time": "2026-01-01T12:05:00",
            "problem_names": ["link_down"],
            "failure_domain": "link",
            "outcome": "success",
        },
    )
    _write_jsonl(
        finished / "nika.jsonl",
        [
            {
                "timestamp": "2026-01-01T12:01:05",
                "level": "INFO",
                "event": "env_start",
                "message": "Lab deployed",
                "data": {"scenario": "dc_clos"},
            },
            {
                "timestamp": "2026-01-01T12:01:08",
                "level": "INFO",
                "event": "failure_injected",
                "message": "Injected link_down",
                "data": {"problem": "link_down"},
            },
            {
                "timestamp": "2026-01-01T12:01:09",
                "level": "INFO",
                "event": "agent_start",
                "message": "Agent started",
            },
            {
                "timestamp": "2026-01-01T12:02:35",
                "level": "INFO",
                "event": "eval_metrics_saved",
                "message": "Metrics written",
            },
        ],
    )
    _write_jsonl(
        finished / "messages.jsonl",
        [
            {
                "timestamp": "2026-01-01T12:01:10",
                "phase": "diagnosis",
                "event": "agent_start",
            },
            {
                "timestamp": "2026-01-01T12:01:12",
                "phase": "diagnosis",
                "event": "tool_start",
                "tool": {"name": "ping"},
                "input": '{"host": "pc2"}',
                "tool_call_id": "call_1",
            },
            {
                "timestamp": "2026-01-01T12:01:14",
                "phase": "diagnosis",
                "event": "tool_end",
                "tool": {"name": "ping"},
                "input": '{"host": "pc2"}',
                "output": "100% packet loss",
                "tool_call_id": "call_1",
            },
            {
                "timestamp": "2026-01-01T12:02:34",
                "phase": "submission",
                "event": "diagnosis_frozen",
                "report": {"root_causes": []},
            },
        ],
    )
    _write_json(
        finished / "ground_truth.json",
        {
            "is_anomaly": True,
            "root_causes": [
                {
                    "resource_id": "link/s1/eth1",
                    "fault_type": "link_down",
                }
            ],
        },
    )
    _write_json(
        finished / "submission.json",
        {
            "is_anomaly": True,
            "root_causes": [
                {
                    "resource_id": "link/s1/eth1",
                    "fault_type": "link_down",
                }
            ],
        },
    )
    _write_json(
        finished / "eval_metrics.json",
        {
            "detection_score": 1.0,
            "rca_f1": 1.0,
            "localization_f1": 1.0,
            "steps": 3,
            "tool_calls": 1,
        },
    )

    running = tmp_path / "20260101-130000-run999"
    running.mkdir()
    _write_json(
        running / "run.json",
        {
            "session_id": "20260101-130000-run999",
            "status": "running",
            "scenario_name": "campus",
            "agent_type": "cli.codex",
            "start_time": "2026-01-01T13:00:00",
        },
    )
    _write_jsonl(
        running / "nika.jsonl",
        [
            {
                "timestamp": "2026-01-01T13:00:01",
                "level": "INFO",
                "event": "env_start",
                "message": "deploying",
            }
        ],
    )
    return tmp_path


class TestAdapters:
    def test_agent_tool_events(self) -> None:
        start = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:00",
                "phase": "diagnosis",
                "event": "tool_start",
                "tool": {"name": "exec"},
                "input": "vtysh -c 'show ip bgp'",
                "tool_call_id": "t1",
            },
            index=0,
        )
        assert start.source == "agent"
        assert start.kind == "tool_call"
        assert start.tool is not None
        assert start.tool.name == "exec"
        assert "vtysh" in str(start.tool.input)

        end = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:01",
                "phase": "diagnosis",
                "event": "tool_end",
                "tool": {"name": "exec"},
                "output": "BGP table",
                "tool_call_id": "t1",
            },
            index=1,
        )
        assert end.kind == "tool_result"

    def test_codex_tool_start_parses_json_io(self) -> None:
        start = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:00",
                "event": "tool_start",
                "tool": {"name": "run_pingmesh_snapshot"},
                "input": '{"count": 2, "targets": ["dhcp_server"]}',
            },
            index=0,
        )
        assert start.tool is not None
        assert start.tool.input == {"count": 2, "targets": ["dhcp_server"]}

        end = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:01",
                "event": "tool_end",
                "tool": {"name": "run_pingmesh_snapshot"},
                "output": '{"endpoints": {"dhcp_server": "10.0.0.1"}}',
            },
            index=1,
        )
        assert end.tool is not None
        assert end.tool.output == {"endpoints": {"dhcp_server": "10.0.0.1"}}

    def test_codex_mcp_item_without_tool_start_keeps_arguments(self) -> None:
        start = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:00",
                "event": "item.started",
                "codex_event": {
                    "item": {
                        "type": "mcp_tool_call",
                        "tool": "run_pingmesh_snapshot",
                        "arguments": {"count": 2, "targets": ["a"]},
                    }
                },
            },
            index=0,
        )
        assert start.kind == "tool_call"
        assert start.tool is not None
        assert start.tool.name == "run_pingmesh_snapshot"
        assert start.tool.input == {"count": 2, "targets": ["a"]}

    def test_load_agent_events_keeps_tool_and_message_rows(
        self, tmp_path: Path
    ) -> None:
        from nika.view.adapters import load_agent_events

        session = tmp_path / "sess"
        session.mkdir()
        rows = [
            {
                "timestamp": "2026-01-01T12:00:00",
                "event": "tool_start",
                "tool": {"name": "run_pingmesh_snapshot"},
                "input": '{"count": 1}',
            },
            {
                "timestamp": "2026-01-01T12:00:01",
                "event": "tool_end",
                "tool": {"name": "run_pingmesh_snapshot"},
                "output": '{"ok": true}',
            },
            {
                "timestamp": "2026-01-01T12:00:02",
                "event": "item.completed",
                "codex_event": {
                    "item": {"type": "agent_message", "text": "hello"}
                },
            },
        ]
        (session / "messages.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        events = load_agent_events(session)
        assert [e.event for e in events] == [
            "tool_start",
            "tool_end",
            "item.completed",
        ]
        assert events[0].tool is not None
        assert events[0].tool.input == {"count": 1}
        assert events[1].tool is not None
        assert events[1].tool.output == {"ok": True}
        assert events[2].kind == "llm"

    def test_nika_lifecycle_event(self) -> None:
        event = adapt_nika_event(
            {
                "timestamp": "2026-01-01T12:00:00",
                "event": "failure_injected",
                "message": "done",
                "data": {"problem": "mtu_mismatch"},
            },
            index=0,
        )
        assert event.source == "nika"
        assert event.kind == "lifecycle"
        assert "mtu_mismatch" in event.summary


class TestTimelineMerge:
    def test_merge_orders_injection_before_tools(self) -> None:
        nika = [
            CanonicalTraceEvent(
                id="nika-0",
                timestamp="2026-01-01T12:01:08",
                source="nika",
                kind="lifecycle",
                title="failure injected",
                event="failure_injected",
            )
        ]
        agent = [
            CanonicalTraceEvent(
                id="agent-0",
                timestamp="2026-01-01T12:01:12",
                source="agent",
                kind="tool_call",
                title="tool ping",
                event="tool_start",
            ),
            CanonicalTraceEvent(
                id="agent-1",
                timestamp="2026-01-01T12:01:08",
                source="agent",
                kind="phase",
                title="agent_start",
                event="agent_start",
            ),
        ]
        merged = merge_timelines(agent, nika)
        assert [e.id for e in merged] == ["nika-0", "agent-1", "agent-0"]
        assert merged[0].source == "nika"


class TestCatalog:
    def test_list_filters_status_and_scenario(self, fixture_root: Path) -> None:
        all_sessions = list_sessions(results_root=fixture_root, status="all")
        assert len(all_sessions) == 2

        finished = list_sessions(results_root=fixture_root, status="finished")
        assert len(finished) == 1
        assert finished[0].session_id == "20260101-120000-abc123"
        assert finished[0].rca_f1 == 1.0
        assert finished[0].artifacts.messages is True

        filtered = list_sessions(
            results_root=fixture_root, status="all", scenario="campus"
        )
        assert len(filtered) == 1
        assert filtered[0].status == "running"

    def test_summarize_running(self, fixture_root: Path) -> None:
        summary = summarize_session_dir(fixture_root / "20260101-130000-run999")
        assert summary is not None
        assert summary.status == "running"
        assert summary.is_benchmark is False

    def test_benchmark_trial_enrichment(self, tmp_path: Path) -> None:
        run_root = tmp_path / "bench-demo-run"
        trials = run_root / "trials"
        trial = trials / "dc_clos__link_down__s__t01"
        trial.mkdir(parents=True)
        _write_json(
            run_root / "run.json",
            {
                "run_id": "job-abc",
                "benchmark_id": "nika-pilot",
                "version": "0.2.0",
                "split": "dev",
                "official": False,
                "n_trials": 2,
                "agent_type": "byo.langgraph",
                "model": "gpt-test",
                "scoring": {"id": "rca_f1"},
            },
        )
        _write_json(
            trial / "run.json",
            {
                "session_id": "dc_clos__link_down__s__t01",
                "status": "finished",
                "case_key": "dc_clos__link_down__s",
                "trial_id": "dc_clos__link_down__s__t01",
                "trial_index": 1,
                "scenario_name": "dc_clos",
                "agent_type": "byo.langgraph",
                "model": "gpt-test",
                "start_time": "2026-01-01T12:00:00",
                "end_time": "2026-01-01T12:05:00",
                "problem_names": ["link_down"],
                "benchmark_fingerprint": json.dumps(
                    {
                        "scenario": "dc_clos",
                        "problem": "link_down",
                        "inject": {"host_name": "leaf0", "intf_name": "eth1"},
                    }
                ),
            },
        )
        summary = summarize_session_dir(trial)
        assert summary is not None
        assert summary.is_benchmark is True
        assert summary.inject_params == {"host_name": "leaf0", "intf_name": "eth1"}
        assert summary.benchmark_id == "nika-pilot"
        assert summary.benchmark_version == "0.2.0"
        assert summary.benchmark_split == "dev"
        assert summary.benchmark_run_id == "job-abc"
        assert summary.benchmark_label == "nika-pilot@0.2.0"
        assert summary.trial_index == 1

        from nika.view.catalog import aggregate_benchmark_runs

        runs = aggregate_benchmark_runs([summary])
        assert len(runs) == 1
        assert runs[0].label == "nika-pilot@0.2.0"
        assert runs[0].session_count == 1

    def test_healthy_baseline_uses_shared_failure_label(self, tmp_path: Path) -> None:
        trial = tmp_path / "bench-run" / "trials" / "dc_clos__healthy__s__t01"
        trial.mkdir(parents=True)
        _write_json(
            trial / "run.json",
            {
                "session_id": "dc_clos__healthy__s__t01",
                "status": "finished",
                "case_key": "dc_clos__healthy__s",
                "trial_index": 1,
                "scenario_name": "dc_clos",
                "scenario_topo_size": "s",
                "problem_names": ["healthy"],
                "start_time": "2026-01-01T12:00:00",
                "end_time": "2026-01-01T12:05:00",
            },
        )
        summary = summarize_session_dir(trial)
        assert summary is not None
        assert summary.problem_names == ["healthy"]
        assert summary.scenario_name == "dc_clos"


class TestViewApi:
    def test_api_smoke(self, fixture_root: Path) -> None:
        app = create_view_app(results_root=fixture_root)
        client = TestClient(app)

        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        listed = client.get("/api/sessions")
        assert listed.status_code == 200
        body = listed.json()
        assert body["total"] == 2

        sid = "20260101-120000-abc123"
        detail = client.get(f"/api/sessions/{sid}")
        assert detail.status_code == 200
        assert detail.json()["scenario_name"] == "dc_clos"

        timeline = client.get(f"/api/sessions/{sid}/timeline")
        assert timeline.status_code == 200
        events = timeline.json()["events"]
        assert events[0]["event"] == "env_start"
        titles = [e["event"] for e in events]
        assert "failure_injected" in titles
        assert "tool_start" in titles
        # Injection precedes first tool call.
        assert titles.index("failure_injected") < titles.index("tool_start")

        scores = client.get(f"/api/sessions/{sid}/scores")
        assert scores.status_code == 200
        payload = scores.json()
        assert payload["eval_metrics"]["rca_f1"] == 1.0
        assert payload["ground_truth"]["is_anomaly"] is True
        assert payload["submission"]["is_anomaly"] is True

        raw = client.get(f"/api/sessions/{sid}/raw/run.json")
        assert raw.status_code == 200
        assert raw.json()["data"]["session_id"] == sid

        missing = client.get("/api/sessions/does-not-exist")
        assert missing.status_code == 404


class TestBindHost:
    def test_allows_loopback_and_all_interfaces(self) -> None:
        from nika.view.serve import validate_bind_host

        assert validate_bind_host("127.0.0.1") == "127.0.0.1"
        assert validate_bind_host("0.0.0.0") == "0.0.0.0"
        assert validate_bind_host("::") == "::"
        assert validate_bind_host("LocalHost") == "localhost"

    def test_rejects_specific_interface_ip(self) -> None:
        from nika.view.serve import validate_bind_host

        with pytest.raises(ValueError, match="not a specific interface IP"):
            validate_bind_host("192.168.1.10")


class TestCatalogSafety:
    def test_find_session_dir_rejects_path_escape(self, tmp_path: Path) -> None:
        from nika.view.catalog import find_session_dir

        root = tmp_path / "results"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        _write_json(outside / "run.json", {"session_id": "leak", "status": "finished"})

        assert find_session_dir("../outside", results_root=root) is None
        assert find_session_dir(str(outside), results_root=root) is None


class TestTimelineMergeAware:
    def test_merge_mixed_naive_and_aware_timestamps(self) -> None:
        nika = [
            CanonicalTraceEvent(
                id="nika-0",
                timestamp="2026-09-10T01:16:14+00:00",
                source="nika",
                kind="lifecycle",
                title="agent start",
                event="agent_start",
            )
        ]
        agent = [
            CanonicalTraceEvent(
                id="agent-0",
                timestamp="2026-09-10T01:16:15",
                source="agent",
                kind="llm",
                title="llm",
                event="llm_start",
            )
        ]
        merged = merge_timelines(agent, nika)
        assert [e.id for e in merged] == ["nika-0", "agent-0"]
