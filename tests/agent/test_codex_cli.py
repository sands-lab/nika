from __future__ import annotations

import asyncio

import pytest

from agent.cli.codex.codex_display import format_codex_event
from agent.cli.codex.codex_worker import (
    CodexSubprocessStallError,
    CodexWorker,
    RECONNECT_STALL_TIMEOUT_S,
    _build_mcp_toml,
    _is_productive_codex_event,
    _reconnect_transport_failed,
)
from agent.utils.phases import DIAGNOSIS, SUBMISSION
from nika.utils.session_store import SessionStore
from tests.agent._assertions import assert_submission_fields
from tests.agent.sandbox_support import SANDBOX_E2E_SUPERSEDED
from tests.support.integration_base import OrderedPipelineTestCase
from tests.support.integration_pipeline import (
    ClabCommonPipelineSteps,
    CommonPipelineSteps,
    _min3clos_prerequisites,
    codex_cli_available,
    load_test_env,
)

load_test_env()
CODEX_MODEL = "gpt-5.4-mini"


class CodexMcpTomlTest:
    """MCP config TOML generation for Codex CLI."""

    def test_includes_noninteractive_approval_defaults(self) -> None:
        toml = _build_mcp_toml(
            {
                "kathara_base_mcp_server": {
                    "command": "python3",
                    "args": ["/path/kathara_base_mcp_server.py"],
                    "env": {"NIKA_SESSION_ID": "sess-123"},
                }
            }
        )
        assert 'approval_policy = "never"' in toml
        assert 'sandbox_mode = "workspace-write"' in toml
        assert 'default_tools_approval_mode = "approve"' in toml
        assert "[mcp_servers.kathara_base_mcp_server]" in toml
        assert 'NIKA_SESSION_ID = "sess-123"' in toml

    def test_approves_each_configured_server(self) -> None:
        toml = _build_mcp_toml(
            {
                "kathara_base_mcp_server": {
                    "command": "python3",
                    "args": ["/path/base.py"],
                },
                "task_mcp_server": {"command": "python3", "args": ["/path/task.py"]},
            }
        )
        assert toml.count('default_tools_approval_mode = "approve"') == 2


class CodexProgressDetectionTest:
    """Codex JSONL progress / reconnect stall detection."""

    def test_productive_events_include_tool_calls(self) -> None:
        event = {
            "type": "item.completed",
            "item": {"type": "mcp_tool_call", "status": "completed"},
        }
        assert _is_productive_codex_event(event)

    def test_reconnect_errors_are_not_productive(self) -> None:
        event = {"type": "error", "message": "Reconnecting... 2/5 (request timed out)"}
        assert not _is_productive_codex_event(event)

    def test_reconnect_exhaustion_detected(self) -> None:
        event = {"type": "error", "message": "Reconnecting... 5/5 (request timed out)"}
        assert _reconnect_transport_failed(event)

    def test_transport_fallback_detected(self) -> None:
        event = {
            "type": "item.completed",
            "item": {
                "type": "error",
                "message": "Falling back from WebSockets to HTTPS transport. request timed out",
            },
        }
        assert _reconnect_transport_failed(event)

    def test_reconnect_failure_triggers_stall(self) -> None:
        worker = CodexWorker(
            session_id="sess-123",
            session_dir="/tmp/sess-123",
            phase="diagnosis",
        )
        loop = asyncio.new_event_loop()
        now = loop.time()
        worker._last_progress_at = now - 10
        worker._reconnect_failure_at = now - RECONNECT_STALL_TIMEOUT_S - 1

        with pytest.raises(CodexSubprocessStallError) as exc_info:
            worker._raise_if_stalled(loop)

        assert exc_info.value.reconnect_failure is True
        loop.close()


class CodexWorkerConfigTest:
    """CodexWorker constructor validation."""

    def test_rejects_invalid_reasoning_effort(self) -> None:
        with pytest.raises(ValueError):
            CodexWorker(
                session_id="sess-123",
                session_dir="/tmp/sess-123",
                phase=DIAGNOSIS,
                reasoning_effort="turbo",
            )


class CodexDisplayTest:
    """Codex JSONL terminal event formatting."""

    def test_agent_message(self) -> None:
        event = {
            "type": "item.completed",
            "item": {
                "id": "item_1",
                "type": "agent_message",
                "text": "BGP session is down.",
            },
        }
        assert "BGP session is down." in (format_codex_event(event) or "")

    def test_mcp_tool_call_lifecycle(self) -> None:
        started = {
            "type": "item.started",
            "item": {
                "type": "mcp_tool_call",
                "server": "kathara_frr_mcp_server",
                "tool": "show_bgp_summary",
                "arguments": {"device": "router1"},
                "status": "in_progress",
            },
        }
        completed = {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "kathara_frr_mcp_server",
                "tool": "show_bgp_summary",
                "status": "completed",
                "result": {"content": [{"type": "text", "text": "neighbor down"}]},
            },
        }
        assert "show_bgp_summary" in (format_codex_event(started) or "")
        assert "neighbor down" in (format_codex_event(completed) or "")

    def test_turn_completed_with_usage(self) -> None:
        event = {
            "type": "turn.completed",
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        rendered = format_codex_event(event)
        assert "in=100" in (rendered or "")
        assert "out=20" in (rendered or "")

    def test_reconnecting_error_is_non_fatal(self) -> None:
        event = {"type": "error", "message": "Reconnecting... 1/5"}
        assert "Reconnecting" in (format_codex_event(event) or "")

    def test_unknown_event_returns_none(self) -> None:
        assert format_codex_event({"type": "some_unknown_type"}) is None


@SANDBOX_E2E_SUPERSEDED
@pytest.mark.skipif(
    not codex_cli_available(), reason="Codex CLI and OpenAI credentials required"
)
class CodexCliAgentPipelineTest(CommonPipelineSteps, OrderedPipelineTestCase):
    """Full pipeline with the Codex CLI agent."""

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_03_run_cli_agent(self) -> None:
        assert self.session_id is not None
        self._run_agent(
            agent_type="cli.codex", model=CODEX_MODEL, max_steps=20
        )
        row = SessionStore().get_session(self.session_id)
        assert row.get("agent_type") == "cli.codex"

    def test_step_04_check_workspace_and_messages(self) -> None:
        assert self.session_dir is not None
        workspace = self.session_dir / "codex_workspace"
        assert workspace.is_dir()
        assert (workspace / ".git").is_dir()
        assert (workspace / ".codex_home").is_dir()
        config_text = (workspace / ".codex_home" / "config.toml").read_text(
            encoding="utf-8"
        )
        assert "NIKA-Session-Id" in config_text
        assert self.session_id in config_text
        assert "[mcp_servers." in config_text
        assert 'default_tools_approval_mode = "approve"' in config_text
        diag_output = workspace / "diagnosis_output.txt"
        assert diag_output.exists()
        assert diag_output.stat().st_size > 0
        messages = self._load_jsonl("messages.jsonl")
        agents = {e["agent"] for e in messages}
        assert DIAGNOSIS in agents
        assert SUBMISSION in agents
        mcp_events = [e for e in messages if e.get("event") == "mcp_config"]
        diag_mcp = next((e for e in mcp_events if e.get("agent") == DIAGNOSIS), None)
        assert diag_mcp is not None
        servers = diag_mcp.get("servers", [])
        assert "kathara_base_mcp_server" in servers
        assert "kathara_frr_mcp_server" in servers
        assert "kathara_bmv2_mcp_server" not in servers
        assert "kathara_telemetry_mcp_server" not in servers
        sub_mcp = next((e for e in mcp_events if e.get("agent") == SUBMISSION), None)
        assert sub_mcp is not None
        assert "task_mcp_server" in sub_mcp.get("servers", [])
        start_events = [e for e in messages if e.get("event") == "subprocess_start"]
        assert len(start_events) >= 2
        codex_events = [e for e in messages if "codex_event" in e]
        assert len(codex_events) > 0
        rendered_count = sum(
            (1 for e in codex_events if format_codex_event(e["codex_event"]))
        )
        assert rendered_count > 0

    def test_step_05_check_submission(self) -> None:
        assert self.session_dir is not None
        assert (self.session_dir / "submission.json").exists()
        assert_submission_fields(self.session_dir)

    def test_step_06_session_close(self) -> None:
        self._step_close_and_verify("cli.codex")

    def test_step_07_eval_metrics(self) -> None:
        self._step_eval_metrics()


@SANDBOX_E2E_SUPERSEDED
@pytest.mark.skipif(
    not (_min3clos_prerequisites() and codex_cli_available()),
    reason="containerlab/gnmic/Docker or Codex CLI credentials not available",
)
class CodexClabPipelineTest(ClabCommonPipelineSteps, OrderedPipelineTestCase):
    """Full containerlab pipeline with the Codex CLI agent."""

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_03_run_cli_agent(self) -> None:
        assert self.session_id is not None
        self._run_agent(
            agent_type="cli.codex", model=CODEX_MODEL, max_steps=20
        )
        row = SessionStore().get_session(self.session_id)
        assert row.get("agent_type") == "cli.codex"

    def test_step_04_check_workspace_and_messages(self) -> None:
        assert self.session_dir is not None
        workspace = self.session_dir / "codex_workspace"
        assert workspace.is_dir()
        config_text = (workspace / ".codex_home" / "config.toml").read_text(
            encoding="utf-8"
        )
        assert "NIKA-Session-Id" in config_text
        assert self.session_id in config_text
        assert "[mcp_servers.kathara_base_mcp_server]" in config_text
        assert "[mcp_servers.kathara_frr_mcp_server]" not in config_text
        messages = self._load_jsonl("messages.jsonl")
        agents = {e["agent"] for e in messages}
        assert DIAGNOSIS in agents
        assert SUBMISSION in agents
        mcp_events = [e for e in messages if e.get("event") == "mcp_config"]
        diag_mcp = next((e for e in mcp_events if e.get("agent") == DIAGNOSIS), None)
        assert diag_mcp is not None
        servers = diag_mcp.get("servers", [])
        assert "kathara_base_mcp_server" in servers
        assert "kathara_frr_mcp_server" not in servers

    def test_step_05_check_submission(self) -> None:
        assert self.session_dir is not None
        assert (self.session_dir / "submission.json").exists()
        assert_submission_fields(self.session_dir)

    def test_step_06_session_close(self) -> None:
        self._step_close_and_verify("cli.codex")

    def test_step_07_eval_metrics(self) -> None:
        self._step_eval_metrics()
