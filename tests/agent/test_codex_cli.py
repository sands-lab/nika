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
from agent.protocols import DIAGNOSIS
from agent.protocols import SUBMISSION
from tests.support.integration_pipeline import load_test_env

load_test_env()


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
        assert "[sandbox_workspace_write]" in toml
        assert "network_access = true" in toml
        assert "experimental_use_rmcp_client" not in toml
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

    def test_requires_http_servers(self) -> None:
        toml = _build_mcp_toml(
            {
                "task_mcp_server": {
                    "transport": "http",
                    "url": "http://host.docker.internal:12345/mcp/task/mcp",
                }
            }
        )
        assert "required = true" in toml


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
            llm_provider="openai",
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
                llm_provider="openai",
            )

    @pytest.mark.parametrize(
        ("phase", "expected_servers"),
        [
            (DIAGNOSIS, {"kathara_base_mcp_server"}),
            (SUBMISSION, {"task_mcp_server"}),
        ],
    )
    def test_writes_only_phase_allowed_mcp_servers(
        self, monkeypatch, tmp_path, phase, expected_servers
    ) -> None:
        monkeypatch.setattr(
            "agent.cli.codex.codex_worker.apply_codex_auth", lambda _path: None
        )
        monkeypatch.setattr(
            "agent.cli.codex.codex_worker.prepare_codex_workspace", lambda _path: None
        )
        monkeypatch.setattr(
            "agent.cli.codex.codex_worker.begin_submission_mcp_phase", lambda _sid: None
        )
        monkeypatch.setattr(
            "agent.cli.codex.codex_worker.load_session_mcp_config",
            lambda *_args, **_kwargs: {
                "kathara_base_mcp_server": {"transport": "http", "url": "http://base"},
                "task_mcp_server": {"transport": "http", "url": "http://task"},
            },
        )
        worker = CodexWorker(
            session_id="sess-123",
            session_dir=tmp_path,
            phase=phase,
            llm_provider="openai",
        )
        worker._setup_workspace()
        config = (worker._codex_home / "config.toml").read_text()
        assert {
            name
            for name in ("kathara_base_mcp_server", "task_mcp_server")
            if name in config
        } == expected_servers


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
