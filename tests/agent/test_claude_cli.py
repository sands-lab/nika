from __future__ import annotations
import pytest
import json
import os
import unittest.mock
from agent.cli.claude.claude_display import (
    format_claude_event,
    should_log_claude_event,
)
from agent.cli.claude.claude_worker import ClaudeWorker, _build_mcp_json
from agent.cli.claude.config import (
    default_claude_model,
    has_env_claude_credentials,
    prepare_claude_subprocess_env,
    use_bare_claude_mode,
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
    claude_cli_available,
    load_test_env,
)

load_test_env()


class ClaudeConfigTest:
    """Claude env model and auth helpers."""

    def test_default_model_reads_anthropic_model(self) -> None:
        with unittest.mock.patch.dict(
            os.environ,
            {"ANTHROPIC_MODEL": "model-a", "CLAUDE_CODE_SUBAGENT_MODEL": "model-b"},
            clear=True,
        ):
            assert default_claude_model() == "model-a"

    def test_default_model_missing_raises(self) -> None:
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError):
                default_claude_model()

    def test_prepare_env_maps_auth_token_to_api_key(self) -> None:
        env = prepare_claude_subprocess_env(
            {"PATH": "/bin", "HOME": "/tmp", "ANTHROPIC_AUTH_TOKEN": "tok"},
            provider="anthropic",
        )
        assert env["ANTHROPIC_API_KEY"] == "tok"

    def test_prepare_env_maps_deepseek_provider(self) -> None:
        env = prepare_claude_subprocess_env(
            {
                "PATH": "/bin",
                "HOME": "/tmp",
                "DEEPSEEK_API_KEY": "sk-ds",
                "OPENAI_API_KEY": "sk-openai",
                "LANGFUSE_SECRET_KEY": "lf",
            },
            provider="deepseek",
        )
        assert env["ANTHROPIC_API_KEY"] == "sk-ds"
        assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
        assert "OPENAI_API_KEY" not in env
        assert "LANGFUSE_SECRET_KEY" not in env

    def test_use_bare_mode_when_env_credentials_present(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "key"}, clear=True
        ):
            assert use_bare_claude_mode()
            assert has_env_claude_credentials()

    def test_subscription_mode_disables_bare(self) -> None:
        with (
            unittest.mock.patch.dict(os.environ, {}, clear=True),
            unittest.mock.patch(
                "agent.cli.claude.config.claude_subscription_mode",
                return_value=True,
            ),
        ):
            assert not use_bare_claude_mode()


class ClaudeMcpJsonTest:
    """MCP config JSON generation for Claude Code CLI."""

    def test_produces_valid_json_with_mcp_servers_key(self) -> None:
        json_str = _build_mcp_json(
            {
                "kathara_base_mcp_server": {
                    "command": "python3",
                    "args": ["/path/base.py"],
                    "env": {"NIKA_SESSION_ID": "sess-abc"},
                }
            }
        )
        config = json.loads(json_str)
        assert "mcpServers" in config
        srv = config["mcpServers"]["kathara_base_mcp_server"]
        assert srv["type"] == "stdio"
        assert srv["command"] == "python3"
        assert srv["args"] == ["/path/base.py"]
        assert srv["env"]["NIKA_SESSION_ID"] == "sess-abc"

    def test_multiple_servers_all_present(self) -> None:
        json_str = _build_mcp_json(
            {
                "kathara_base_mcp_server": {
                    "command": "python3",
                    "args": ["/path/base.py"],
                },
                "task_mcp_server": {"command": "python3", "args": ["/path/task.py"]},
            }
        )
        config = json.loads(json_str)
        assert "kathara_base_mcp_server" in config["mcpServers"]
        assert "task_mcp_server" in config["mcpServers"]

    def test_server_without_env_omits_env_key(self) -> None:
        json_str = _build_mcp_json(
            {"task_mcp_server": {"command": "python3", "args": ["/path/task.py"]}}
        )
        config = json.loads(json_str)
        assert "env" not in config["mcpServers"]["task_mcp_server"]


class ClaudeWorkerConfigTest:
    """ClaudeWorker constructor validation."""

    def test_rejects_invalid_phase(self) -> None:
        with pytest.raises(ValueError):
            ClaudeWorker(
                session_id="sess-123",
                session_dir="/tmp/sess-123",
                phase="invalid_phase",
            )


class ClaudeDisplayTest:
    """Claude Code stream-json event formatting."""

    sample_model = "model-a"

    def test_system_init_event_with_string_servers(self) -> None:
        event = {
            "type": "system",
            "subtype": "init",
            "model": self.sample_model,
            "mcp_servers": ["kathara_base_mcp_server"],
        }
        rendered = format_claude_event(event)
        assert self.sample_model in (rendered or "")
        assert "kathara_base_mcp_server" in (rendered or "")

    def test_system_init_event_with_dict_servers(self) -> None:
        event = {
            "type": "system",
            "subtype": "init",
            "model": self.sample_model,
            "mcp_servers": [{"name": "kathara_base_mcp_server", "status": "connected"}],
        }
        rendered = format_claude_event(event)
        assert "kathara_base_mcp_server" in (rendered or "")

    def test_system_init_without_servers(self) -> None:
        event = {
            "type": "system",
            "subtype": "init",
            "model": self.sample_model,
            "mcp_servers": [],
        }
        rendered = format_claude_event(event)
        assert rendered is not None
        assert "mcp:" not in (rendered or "")

    def test_thinking_tokens_skipped(self) -> None:
        event = {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 42}
        assert format_claude_event(event) is None
        assert not should_log_claude_event(event)

    def test_should_log_keeps_meaningful_events(self) -> None:
        assert should_log_claude_event(
            {"type": "system", "subtype": "init", "model": "x"}
        )
        assert should_log_claude_event(
            {"type": "assistant", "message": {"content": [{"type": "text"}]}}
        )
        assert should_log_claude_event(
            {"type": "result", "is_error": False, "usage": {}}
        )

    def test_assistant_text_message(self) -> None:
        event = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "BGP peer is unreachable."}]
            },
        }
        rendered = format_claude_event(event)
        assert "BGP peer is unreachable." in (rendered or "")

    def test_assistant_tool_use_block(self) -> None:
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "mcp__kathara_frr_mcp_server__frr_show_ip_route",
                        "input": {"host_name": "r1"},
                    }
                ]
            },
        }
        rendered = format_claude_event(event)
        assert "mcp__kathara_frr_mcp_server__frr_show_ip_route" in (rendered or "")

    def test_result_success(self) -> None:
        event = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Diagnosis complete.",
            "usage": {"input_tokens": 500, "output_tokens": 80},
        }
        rendered = format_claude_event(event)
        assert "in=500" in (rendered or "")
        assert "out=80" in (rendered or "")

    def test_result_error(self) -> None:
        event = {"type": "result", "is_error": True, "result": "Not logged in"}
        rendered = format_claude_event(event)
        assert "Not logged in" in (rendered or "")

    def test_unknown_type_returns_none(self) -> None:
        assert format_claude_event({"type": "some_unknown"}) is None


@SANDBOX_E2E_SUPERSEDED
@pytest.mark.skipif(
    not claude_cli_available(), reason="Claude Code CLI and credentials required"
)
class ClaudeAgentPipelineTest(CommonPipelineSteps, OrderedPipelineTestCase):
    """Full pipeline with the Claude Code CLI agent."""

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_03_run_claude_agent(self) -> None:
        assert self.session_id is not None
        self._run_agent(agent_type="cli.claude", max_steps=20)
        row = SessionStore().get_session(self.session_id)
        assert row.get("agent_type") == "cli.claude"

    def test_step_04_check_workspace_and_messages(self) -> None:
        assert self.session_dir is not None
        workspace = self.session_dir / "claude_workspace"
        assert workspace.is_dir()
        diag_config = workspace / "diagnosis_mcp_config.json"
        sub_config = workspace / "submission_mcp_config.json"
        assert diag_config.exists()
        assert sub_config.exists()
        diag_cfg = json.loads(diag_config.read_text())
        assert "mcpServers" in diag_cfg
        assert "kathara_base_mcp_server" in diag_cfg["mcpServers"]
        assert "kathara_frr_mcp_server" in diag_cfg["mcpServers"]
        assert "kathara_bmv2_mcp_server" not in diag_cfg["mcpServers"]
        sub_cfg = json.loads(sub_config.read_text())
        assert "task_mcp_server" in sub_cfg["mcpServers"]
        diag_cfg_str = diag_config.read_text()
        assert "NIKA-Session-Id" in diag_cfg_str
        assert self.session_id in diag_cfg_str
        messages = self._load_jsonl("messages.jsonl")
        agents = {e["agent"] for e in messages}
        assert DIAGNOSIS in agents
        assert SUBMISSION in agents
        mcp_events = [e for e in messages if e.get("event") == "mcp_config"]
        diag_mcp = next((e for e in mcp_events if e.get("agent") == DIAGNOSIS), None)
        assert diag_mcp is not None
        assert "kathara_base_mcp_server" in diag_mcp.get("servers", [])
        assert "kathara_frr_mcp_server" in diag_mcp.get("servers", [])
        sub_mcp = next((e for e in mcp_events if e.get("agent") == SUBMISSION), None)
        assert sub_mcp is not None
        assert "task_mcp_server" in sub_mcp.get("servers", [])
        start_events = [e for e in messages if e.get("event") == "subprocess_start"]
        assert len(start_events) >= 2
        claude_events = [e for e in messages if "claude_event" in e]
        assert len(claude_events) > 0
        rendered_count = sum(
            (1 for e in claude_events if format_claude_event(e["claude_event"]))
        )
        assert rendered_count > 0
        result_events = [
            e for e in claude_events if e["claude_event"].get("type") == "result"
        ]
        assert result_events

    def test_step_05_check_submission(self) -> None:
        assert self.session_dir is not None
        assert (self.session_dir / "submission.json").exists()
        assert_submission_fields(self.session_dir)

    def test_step_06_session_close(self) -> None:
        self._step_close_and_verify("cli.claude")

    def test_step_07_eval_metrics(self) -> None:
        self._step_eval_metrics()


@SANDBOX_E2E_SUPERSEDED
@pytest.mark.skipif(
    not (_min3clos_prerequisites() and claude_cli_available()),
    reason="containerlab/gnmic/Docker or Claude CLI credentials not available",
)
class ClaudeClabPipelineTest(ClabCommonPipelineSteps, OrderedPipelineTestCase):
    """Full containerlab pipeline with the Claude Code CLI agent."""

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_03_run_claude_agent(self) -> None:
        assert self.session_id is not None
        self._run_agent(agent_type="cli.claude", max_steps=20)
        row = SessionStore().get_session(self.session_id)
        assert row.get("agent_type") == "cli.claude"

    def test_step_04_check_workspace_and_messages(self) -> None:
        assert self.session_dir is not None
        workspace = self.session_dir / "claude_workspace"
        assert workspace.is_dir()
        diag_config = workspace / "diagnosis_mcp_config.json"
        sub_config = workspace / "submission_mcp_config.json"
        assert diag_config.exists()
        assert sub_config.exists()
        diag_cfg = json.loads(diag_config.read_text())
        assert "mcpServers" in diag_cfg
        assert "kathara_base_mcp_server" in diag_cfg["mcpServers"]
        assert "kathara_frr_mcp_server" not in diag_cfg["mcpServers"]
        assert "kathara_bmv2_mcp_server" not in diag_cfg["mcpServers"]
        sub_cfg = json.loads(sub_config.read_text())
        assert "task_mcp_server" in sub_cfg["mcpServers"]
        messages = self._load_jsonl("messages.jsonl")
        agents = {e["agent"] for e in messages}
        assert DIAGNOSIS in agents
        assert SUBMISSION in agents
        mcp_events = [e for e in messages if e.get("event") == "mcp_config"]
        diag_mcp = next((e for e in mcp_events if e.get("agent") == DIAGNOSIS), None)
        assert diag_mcp is not None
        assert "kathara_base_mcp_server" in diag_mcp.get("servers", [])
        assert "kathara_frr_mcp_server" not in diag_mcp.get("servers", [])

    def test_step_05_check_submission(self) -> None:
        assert self.session_dir is not None
        assert (self.session_dir / "submission.json").exists()
        assert_submission_fields(self.session_dir)

    def test_step_06_session_close(self) -> None:
        self._step_close_and_verify("cli.claude")

    def test_step_07_eval_metrics(self) -> None:
        self._step_eval_metrics()
