"""Unit tests for the session viewer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from nika.inspect.adapters import adapt_agent_event, adapt_nika_event
from nika.inspect.catalog import discover_sessions, list_sessions, summarize_session_dir
from nika.inspect.models import CanonicalTraceEvent
from nika.inspect.server import create_inspect_app
from nika.inspect.timeline import merge_timelines

pytestmark = pytest.mark.unit


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

    def test_llm_retry_and_end_error_are_llm_kind(self) -> None:
        retry = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:00",
                "phase": "diagnosis",
                "event": "llm_retry",
                "error": "Request timed out.",
                "failed_attempt": 1,
                "next_attempt": 2,
                "max_retries": 2,
                "run_id": "r1",
            },
            index=0,
        )
        assert retry.kind == "llm"
        assert retry.event == "llm_retry"
        assert "timed out" in retry.summary.lower()

        err = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:01",
                "phase": "diagnosis",
                "event": "llm_end_error",
                "error": "APITimeoutError",
                "run_id": "r1",
            },
            index=1,
        )
        assert err.kind == "llm"
        assert err.event == "llm_end_error"

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
        from nika.inspect.adapters import load_agent_events

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
                "codex_event": {"item": {"type": "agent_message", "text": "hello"}},
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

    def test_codex_error_summarizes_without_embedded_transcript(self) -> None:
        """Provider errors that echo the full chat must not blow up the timeline."""
        messages = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "x" * 200}],
            }
            for _ in range(50)
        ]
        inner = (
            "197 validation errors:\n"
            "  {'type': 'string_type', 'loc': ('body', 'input', 'str'), "
            "'msg': 'Input should be a valid string', 'input': " + repr(messages) + "}"
        )
        blob = json.dumps(
            {"error": {"message": inner, "type": "invalid_request_error"}}
        )
        assert len(blob) > 5_000

        event = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:00",
                "phase": "diagnosis",
                "event": "error",
                "codex_event": {"type": "error", "message": blob},
            },
            index=0,
        )
        assert event.event == "error"
        assert "197 validation errors" in event.summary
        assert "Input should be a valid string" in event.summary
        assert "input_text" not in event.summary
        raw_msg = (event.raw.get("codex_event") or {}).get("message")
        assert isinstance(raw_msg, str)
        assert len(raw_msg) < len(blob)
        assert "chars total]" in raw_msg

        failed = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:01",
                "phase": "diagnosis",
                "event": "turn.failed",
                "codex_event": {
                    "type": "turn.failed",
                    "error": {"message": blob},
                },
            },
            index=1,
        )
        assert failed.event == "turn.failed"
        assert "Input should be a valid string" in failed.summary

    def test_codex_item_error_uses_message_field(self) -> None:
        event = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:00",
                "event": "item.completed",
                "codex_event": {
                    "item": {
                        "type": "error",
                        "message": "Model metadata for `qwen-local` not found.",
                    }
                },
            },
            index=0,
        )
        assert event.title == "warning"
        assert event.kind == "system"
        assert "qwen-local" in event.summary

    def test_codex_turn_failed_is_llm_kind(self) -> None:
        started = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:00",
                "phase": "diagnosis",
                "event": "turn.started",
                "codex_event": {"type": "turn.started"},
            },
            index=0,
        )
        assert started.kind == "llm"
        assert started.event == "turn.started"

        failed = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:01",
                "phase": "diagnosis",
                "event": "turn.failed",
                "codex_event": {
                    "type": "turn.failed",
                    "error": {"message": "stream disconnected before completion"},
                },
            },
            index=1,
        )
        assert failed.kind == "llm"
        assert failed.event == "turn.failed"
        assert "stream disconnected" in failed.summary

    def test_codex_bookkeeping_and_reconnect_are_system(self) -> None:
        for event_name, title in (
            ("mcp_config", "mcp_config"),
            ("subprocess_start", "subprocess_start"),
            ("thread.started", "thread started"),
        ):
            adapted = adapt_agent_event(
                {
                    "timestamp": "2026-01-01T12:00:00",
                    "phase": "diagnosis",
                    "event": event_name,
                },
                index=0,
            )
            assert adapted.kind == "system", event_name
            assert adapted.title == title

        reconnect = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:01",
                "phase": "diagnosis",
                "event": "error",
                "codex_event": {
                    "type": "error",
                    "message": "Reconnecting... 1/5 (stream disconnected)",
                },
            },
            index=1,
        )
        assert reconnect.kind == "system"
        assert reconnect.title == "error"
        assert "Reconnecting" in reconnect.summary

        sub = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:02",
                "phase": "diagnosis",
                "event": "subprocess_error",
                "returncode": 1,
                "stderr": (
                    "Reading additional input from stdin...\n"
                    "2026-01-01T12:00:02Z ERROR codex_core::tools::router: "
                    "error=unsupported call: frr_show_ip_route\n"
                ),
            },
            index=2,
        )
        assert sub.kind == "system"
        assert sub.title == "subprocess_error"
        assert "unsupported call: frr_show_ip_route" in sub.summary
        assert "Reading additional input" not in sub.summary

    def test_claude_stream_json_tool_and_text(self) -> None:
        text = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:00",
                "phase": "diagnosis",
                "event": "assistant",
                "claude_event": {
                    "type": "assistant",
                    "message": {
                        "model": "claude-test",
                        "content": [{"type": "text", "text": "Checking link state"}],
                    },
                },
            },
            index=0,
        )
        assert text.kind == "llm"
        assert "Checking link state" in text.summary

        start = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:01",
                "phase": "diagnosis",
                "event": "assistant",
                "claude_event": {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "call_1",
                                "name": "mcp__kathara__ethtool",
                                "input": {"host_name": "s1", "intf_name": "eth0"},
                            }
                        ]
                    },
                },
            },
            index=1,
        )
        assert start.kind == "tool_call"
        assert start.tool is not None
        assert start.tool.name == "mcp__kathara__ethtool"
        assert start.tool.tool_call_id == "call_1"
        assert start.tool.input == {"host_name": "s1", "intf_name": "eth0"}

        end = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:02",
                "phase": "diagnosis",
                "event": "user",
                "claude_event": {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call_1",
                                "content": "Link detected: no",
                            }
                        ],
                    },
                },
            },
            index=2,
        )
        assert end.kind == "tool_result"
        assert end.tool is not None
        assert end.tool.tool_call_id == "call_1"
        assert end.tool.output == "Link detected: no"

        err = adapt_agent_event(
            {
                "timestamp": "2026-01-01T12:00:03",
                "event": "user",
                "claude_event": {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "call_2",
                                "content": "NIKA access denied",
                                "is_error": True,
                            }
                        ]
                    },
                },
            },
            index=3,
        )
        assert err.kind == "tool_error"
        assert err.tool is not None
        assert err.tool.output == "NIKA access denied"
        assert err.tool.error == "NIKA access denied"

    def test_claude_string_message_tool_result(self) -> None:
        """Claude sometimes logs tool errors as a string message + tool_use_result."""
        end = adapt_agent_event(
            {
                "timestamp": "2026-09-21T15:47:41.860823+00:00",
                "phase": "diagnosis",
                "event": "user",
                "claude_event": {
                    "type": "user",
                    "message": (
                        "Error executing tool frr_show_ip_route: "
                        "Session 'x' is not running."
                    ),
                    "parent_tool_use_id": None,
                    "tool_use_result": (
                        "Error: Error executing tool frr_show_ip_route: "
                        "Session 'x' is not running."
                    ),
                },
            },
            index=0,
        )
        assert end.kind == "tool_error"
        assert end.tool is not None
        assert end.tool.output is not None
        assert "frr_show_ip_route" in str(end.tool.output)
        assert end.tool.error is not None

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

    def test_discover_skips_session_with_invalid_metrics(self, tmp_path: Path) -> None:
        good = tmp_path / "good"
        good.mkdir()
        _write_json(good / "run.json", {"session_id": "good", "status": "running"})
        bad = tmp_path / "bad"
        bad.mkdir()
        _write_json(bad / "run.json", {"session_id": "bad", "status": "finished"})
        _write_json(bad / "eval_metrics.json", {"rca_f1": {"nested": True}})
        items = discover_sessions(results_root=tmp_path)
        assert [s.session_id for s in items] == ["good"]

    def test_summarize_running(self, fixture_root: Path) -> None:
        summary = summarize_session_dir(fixture_root / "20260101-130000-run999")
        assert summary is not None
        assert summary.status == "running"
        assert summary.is_benchmark is False

    def test_summarize_aborted_and_error_status(self, tmp_path: Path) -> None:
        aborted = tmp_path / "aborted"
        aborted.mkdir()
        _write_json(
            aborted / "run.json",
            {
                "session_id": "aborted",
                "status": "aborted",
                "end_time": "2026-01-01T12:00:00",
            },
        )
        errored = tmp_path / "errored"
        errored.mkdir()
        _write_json(
            errored / "run.json",
            {
                "session_id": "errored",
                "status": "error",
                "end_time": "2026-01-01T12:00:00",
            },
        )
        interrupted = tmp_path / "interrupted"
        interrupted.mkdir()
        _write_json(
            interrupted / "run.json",
            {
                "session_id": "interrupted",
                "status": "interrupted",
                "end_time": "2026-01-01T12:00:00",
            },
        )

        assert summarize_session_dir(aborted).status == "aborted"
        assert summarize_session_dir(errored).status == "error"
        # Legacy progress wording maps to aborted.
        assert summarize_session_dir(interrupted).status == "aborted"

        assert {
            s.session_id for s in list_sessions(results_root=tmp_path, status="aborted")
        } == {
            "aborted",
            "interrupted",
        }
        assert [
            s.session_id for s in list_sessions(results_root=tmp_path, status="error")
        ] == ["errored"]

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

        from nika.inspect.catalog import aggregate_benchmark_runs

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


class TestLiveProgress:
    def test_list_skips_corrupt_and_filters_status(self, tmp_path: Path) -> None:
        from nika.inspect.live_progress import list_benchmark_progress
        from nika.workflows.benchmark.run_progress import write_progress

        runs_dir = tmp_path / "benchmark_runs"
        job = tmp_path / "results" / "job-a"
        job.mkdir(parents=True)
        write_progress(
            "run-live",
            result_dir=job,
            status="running",
            total_trials=10,
            completed_trials=2,
            pending_trials=8,
            benchmark_id="nika-bench",
            version="0.2.0",
            agent_type="byo.langgraph",
            model="test-model",
            runs_dir=runs_dir,
        )
        write_progress(
            "run-done",
            result_dir=tmp_path / "results" / "job-b",
            status="finished",
            total_trials=3,
            completed_trials=3,
            pending_trials=0,
            runs_dir=runs_dir,
        )
        (runs_dir / "broken.json").write_text("{not-json", encoding="utf-8")
        (runs_dir / "empty.json").write_text("{}\n", encoding="utf-8")

        running = list_benchmark_progress(runs_dir=runs_dir, status="running")
        assert len(running) == 1
        assert running[0].run_id == "run-live"
        assert running[0].completed_trials == 2
        assert running[0].benchmark_id == "nika-bench"

        all_runs = list_benchmark_progress(runs_dir=runs_dir, status=None)
        assert {r.run_id for r in all_runs} == {"run-live", "run-done"}

    def test_path_overlap_matches_job_and_descendants(self, tmp_path: Path) -> None:
        from nika.inspect.live_progress import list_benchmark_progress
        from nika.workflows.benchmark.run_progress import write_progress

        runs_dir = tmp_path / "benchmark_runs"
        job = (tmp_path / "results" / "job-a").resolve()
        job.mkdir(parents=True)
        write_progress(
            "run-live",
            result_dir=job,
            status="running",
            total_trials=4,
            completed_trials=1,
            pending_trials=3,
            runs_dir=runs_dir,
        )

        assert len(list_benchmark_progress(runs_dir=runs_dir, under=job)) == 1
        assert (
            len(list_benchmark_progress(runs_dir=runs_dir, under=job / "trials")) == 1
        )
        parent = tmp_path / "results"
        assert len(list_benchmark_progress(runs_dir=runs_dir, under=parent)) == 1
        other = tmp_path / "results" / "other"
        other.mkdir()
        assert list_benchmark_progress(runs_dir=runs_dir, under=other) == []

    def test_api_benchmark_progress(self, tmp_path: Path, monkeypatch) -> None:
        from nika.workflows.benchmark.run_progress import write_progress

        runs_dir = tmp_path / "benchmark_runs"
        job = tmp_path / "job"
        job.mkdir()
        write_progress(
            "api-run",
            result_dir=job,
            status="running",
            total_trials=5,
            completed_trials=1,
            pending_trials=4,
            runs_dir=runs_dir,
        )
        monkeypatch.setattr("nika.inspect.live_progress.BENCHMARK_RUNS_DIR", runs_dir)

        app = create_inspect_app(results_root=job)
        client = TestClient(app)
        resp = client.get("/api/benchmark-progress")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["runs"][0]["run_id"] == "api-run"
        assert body["runs"][0]["completed_trials"] == 1

        # Unrelated under path → empty
        other = tmp_path / "other"
        other.mkdir()
        empty = client.get(
            "/api/benchmark-progress", params={"under": str(other.resolve())}
        )
        assert empty.status_code == 200
        assert empty.json()["total"] == 0


class TestDeleteSessionResult:
    def test_deletes_finished_session(self, tmp_path: Path) -> None:
        from nika.inspect.catalog import delete_session_result

        root = tmp_path / "results"
        trial = root / "trials" / "case__t01"
        trial.mkdir(parents=True)
        _write_json(
            trial / "run.json",
            {"session_id": "case__t01", "status": "finished"},
        )
        (trial / "messages.jsonl").write_text("{}\n", encoding="utf-8")
        deleted = delete_session_result(trial, results_root=root)
        assert deleted == trial.resolve()
        assert not trial.exists()

    def test_refuses_running_session(self, tmp_path: Path) -> None:
        from nika.inspect.catalog import delete_session_result

        root = tmp_path / "results"
        trial = root / "sess"
        trial.mkdir(parents=True)
        _write_json(trial / "run.json", {"session_id": "sess", "status": "running"})
        with pytest.raises(ValueError, match="running"):
            delete_session_result(trial, results_root=root)
        assert trial.is_dir()

    def test_refuses_path_outside_results_root(self, tmp_path: Path) -> None:
        from nika.inspect.catalog import delete_session_result

        root = tmp_path / "results"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        _write_json(outside / "run.json", {"session_id": "x", "status": "finished"})
        with pytest.raises(ValueError, match="outside"):
            delete_session_result(outside, results_root=root)

    def test_api_delete(self, tmp_path: Path) -> None:
        root = tmp_path / "results"
        trial = root / "done"
        trial.mkdir(parents=True)
        _write_json(
            trial / "run.json",
            {
                "session_id": "done",
                "status": "finished",
                "scenario_name": "dc_clos",
                "start_time": "2026-01-01T12:00:00",
                "end_time": "2026-01-01T12:05:00",
            },
        )
        app = create_inspect_app(results_root=root)
        client = TestClient(app)
        resp = client.delete("/api/sessions/done")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert not trial.exists()
        missing = client.get("/api/sessions/done")
        assert missing.status_code == 404


class TestViewApi:
    def test_api_smoke(self, fixture_root: Path) -> None:
        app = create_inspect_app(results_root=fixture_root)
        client = TestClient(app)

        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        roots = client.get("/api/roots")
        assert roots.status_code == 200
        roots_body = roots.json()
        assert roots_body["base_root"] == str(fixture_root.resolve())
        assert roots_body["selected_root"] == "."
        assert any(r["id"] == "." for r in roots_body["roots"])

        browse = client.get("/api/browse")
        assert browse.status_code == 200
        browse_body = browse.json()
        assert browse_body["path"] == str(fixture_root.resolve())
        assert isinstance(browse_body["entries"], list)

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
        from nika.inspect.serve import validate_bind_host

        assert validate_bind_host("127.0.0.1") == "127.0.0.1"
        assert validate_bind_host("0.0.0.0") == "0.0.0.0"
        assert validate_bind_host("::") == "::"
        assert validate_bind_host("LocalHost") == "localhost"

    def test_rejects_specific_interface_ip(self) -> None:
        from nika.inspect.serve import validate_bind_host

        with pytest.raises(ValueError, match="not a specific interface IP"):
            validate_bind_host("192.168.1.10")


class TestBackgroundInspect:
    def test_start_inspect_background_serves_and_is_idempotent(
        self, tmp_path: Path
    ) -> None:
        import nika.inspect.serve as serve
        import urllib.request

        # Isolate module globals so other tests / prior runs do not collide.
        with serve._background_lock:
            if serve._background_server is not None:
                serve._background_server.should_exit = True
            serve._background_url = None
            serve._background_server = None

        url = serve.start_inspect_background(result_dir=tmp_path, port=0)
        assert url.startswith("http://127.0.0.1:")
        assert url.endswith("/")
        assert serve.start_inspect_background(result_dir=tmp_path, port=0) == url

        with urllib.request.urlopen(url + "api/sessions", timeout=2) as resp:
            assert resp.status == 200
            payload = json.loads(resp.read().decode())
        assert "sessions" in payload

        assert serve._background_server is not None
        serve._background_server.should_exit = True
        with serve._background_lock:
            serve._background_url = None
            serve._background_server = None


class TestCatalogSafety:
    def test_find_session_dir_rejects_path_escape(self, tmp_path: Path) -> None:
        from nika.inspect.catalog import find_session_dir

        root = tmp_path / "results"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        _write_json(outside / "run.json", {"session_id": "leak", "status": "finished"})

        assert find_session_dir("../outside", results_root=root) is None
        assert find_session_dir(str(outside), results_root=root) is None

    def test_resolve_results_selection_allows_symlink_child(
        self, tmp_path: Path
    ) -> None:
        from nika.inspect.catalog import (
            list_selectable_roots,
            resolve_results_selection,
        )

        base = tmp_path / "results"
        base.mkdir()
        outside = tmp_path / "outside-run"
        trial = outside / "sess"
        trial.mkdir(parents=True)
        _write_json(trial / "run.json", {"session_id": "sess", "status": "finished"})
        (base / "linked").symlink_to(outside)

        roots = list_selectable_roots(base)
        assert any(r["id"] == "linked" for r in roots)
        assert any(r["path"].endswith("/results/linked") for r in roots)

        selected = resolve_results_selection(base, "linked")
        assert selected == base / "linked"
        assert selected.is_dir()

        with pytest.raises(ValueError, match="Invalid results folder"):
            resolve_results_selection(base, "../outside-run")

    def test_resolve_results_selection_nested_and_absolute(
        self, tmp_path: Path
    ) -> None:
        from nika.inspect.catalog import resolve_results_selection

        base = tmp_path / "results"
        nested = base / "run-a" / "trials"
        nested.mkdir(parents=True)
        other = tmp_path / "other-results"
        other.mkdir()

        assert resolve_results_selection(base, "run-a/trials") == nested
        assert resolve_results_selection(base, str(other)) == other.resolve()
        with pytest.raises(ValueError, match="not found"):
            resolve_results_selection(base, str(tmp_path / "missing"))


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


def _finished_session(path: Path, session_id: str) -> Path:
    path.mkdir(parents=True)
    _write_json(path / "run.json", {"session_id": session_id, "status": "finished"})
    return path


class TestAccessPolicy:
    def test_loopback_bind_rejects_foreign_host_header(self, tmp_path: Path) -> None:
        app = create_inspect_app(results_root=tmp_path, bind_host="127.0.0.1")
        client = TestClient(app, base_url="http://127.0.0.1:7580")
        assert client.get("/api/health").status_code == 200
        rebound = client.get("/api/health", headers={"host": "evil.example:7580"})
        assert rebound.status_code == 403

    def test_cross_origin_delete_refused(self, tmp_path: Path) -> None:
        trial = _finished_session(tmp_path / "done", "done")
        client = TestClient(create_inspect_app(results_root=tmp_path))
        resp = client.delete(
            "/api/sessions/done", headers={"origin": "http://evil.example"}
        )
        assert resp.status_code == 403
        assert trial.is_dir()

    def test_all_interfaces_bind_is_read_only_and_rooted(self, tmp_path: Path) -> None:
        root = tmp_path / "results"
        trial = _finished_session(root / "done", "done")
        outside = tmp_path / "outside"
        outside.mkdir()
        client = TestClient(create_inspect_app(results_root=root, bind_host="0.0.0.0"))
        assert client.delete("/api/sessions/done").status_code == 403
        assert trial.is_dir()
        assert (
            client.get("/api/browse", params={"path": str(outside)}).status_code == 400
        )
        assert (
            client.get("/api/sessions", params={"root": str(outside)}).status_code
            == 400
        )

    def test_job_dir_is_not_a_session(self, tmp_path: Path) -> None:
        job = tmp_path / "job"
        _finished_session(job / "trials" / "t1", "t1")
        _write_json(job / "run.json", {"run_id": "job", "status": "finished"})
        client = TestClient(create_inspect_app(results_root=tmp_path))
        assert client.get("/api/sessions/job").status_code == 404
        assert client.delete("/api/sessions/job").status_code == 404
        assert (job / "trials" / "t1").is_dir()

    def test_ambiguous_bare_id_conflicts(self, tmp_path: Path) -> None:
        _finished_session(tmp_path / "a" / "x", "same")
        _finished_session(tmp_path / "b" / "y", "same")
        client = TestClient(create_inspect_app(results_root=tmp_path))
        assert client.delete("/api/sessions/same").status_code == 409
        assert (tmp_path / "a" / "x").is_dir() and (tmp_path / "b" / "y").is_dir()


class TestTolerantParsing:
    def test_odd_fields_and_partial_writes_do_not_fail(self, tmp_path: Path) -> None:
        trial = _finished_session(tmp_path / "live", "live")
        rows = [
            {"event": "llm_start", "timestamp": 1756720800, "phase": {"n": 1}},
            {"event": "tool_error", "tool": {"name": "t"}, "error": {}},
        ]
        body = (
            "\n".join(json.dumps(r) for r in rows).encode() + b'\n{"event": "x\xe2\x82'
        )
        (trial / "messages.jsonl").write_bytes(body)
        (trial / "eval_metrics.json").write_text('{"a":', encoding="utf-8")
        client = TestClient(create_inspect_app(results_root=tmp_path))
        timeline = client.get("/api/sessions/live/timeline")
        assert timeline.status_code == 200
        first = timeline.json()["events"][0]
        assert first["timestamp"].startswith("2025-09-01T10:00:00")
        assert first["phase"] is None
        assert client.get("/api/sessions/live/raw/messages.jsonl").status_code == 200
        assert client.get("/api/sessions/live/raw/eval_metrics.json").status_code == 200


class TestRunningSessionSecrets:
    def test_ground_truth_and_scores_hidden_until_session_stops(
        self, tmp_path: Path
    ) -> None:
        trial = tmp_path / "live"
        trial.mkdir()
        _write_json(trial / "run.json", {"session_id": "live", "status": "running"})
        _write_json(trial / "ground_truth.json", {"root_causes": ["secret"]})
        _write_json(trial / "eval_metrics.json", {"rca_f1": 1.0})
        client = TestClient(create_inspect_app(results_root=tmp_path))

        assert client.get("/api/sessions/live/raw/ground_truth.json").status_code == 403
        assert client.get("/api/sessions/live/raw/eval_metrics.json").status_code == 403
        scores = client.get("/api/sessions/live/scores").json()
        assert scores["ground_truth"] is None and scores["eval_metrics"] is None
        assert client.get("/api/sessions/live").json()["rca_f1"] is None

        _write_json(trial / "run.json", {"session_id": "live", "status": "finished"})
        raw = client.get("/api/sessions/live/raw/ground_truth.json")
        assert raw.json()["data"] == {"root_causes": ["secret"]}
        assert client.get("/api/sessions/live").json()["rca_f1"] == 1.0


class TestSymlinkedSessionKey:
    def test_key_under_symlinked_child_resolves_without_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nika.inspect import catalog

        base = tmp_path / "results"
        base.mkdir()
        outside = tmp_path / "elsewhere"
        _finished_session(outside / "run1" / "sess", "sess")
        (base / "linked").symlink_to(outside)

        def _no_scan(_root):
            raise AssertionError("session key lookup must not scan the tree")

        monkeypatch.setattr(catalog, "iter_session_dirs", _no_scan)
        found = catalog.find_session_dir("linked/run1/sess", results_root=base)
        assert found == base.absolute() / "linked" / "run1" / "sess"
