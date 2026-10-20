from __future__ import annotations
import pytest
import json
import os
import unittest.mock

from agent.cli.claude.claude_worker import ClaudeWorker, _build_mcp_json
from agent.cli.claude.config import claude_env, use_bare_claude_mode

pytestmark = pytest.mark.unit


class ClaudeConfigTest:
    """Claude Code settings from the sandbox environment."""

    def test_api_key_mode_is_bare_and_aliases_the_auth_token(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {"ANTHROPIC_API_KEY": "placeholder"}, clear=True
        ):
            assert use_bare_claude_mode()
            env = claude_env(max_tokens=512)
        assert env["ANTHROPIC_AUTH_TOKEN"] == "placeholder"
        assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "512"
        assert env["DISABLE_AUTOUPDATER"] == "1"

    def test_subscription_mode_disables_bare(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "proxy-token-x"}, clear=True
        ):
            assert not use_bare_claude_mode()
            assert "ANTHROPIC_AUTH_TOKEN" not in claude_env()
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            assert not use_bare_claude_mode()


class ClaudeMcpJsonTest:
    """MCP config JSON generation for Claude Code CLI."""

    def test_produces_valid_json_with_mcp_servers_key(self) -> None:
        json_str = _build_mcp_json(
            {
                "kathara_base_mcp_server": {
                    "command": "python3",
                    "args": ["/path/base.py"],
                    "env": {"SESSION": "sess-abc"},
                }
            }
        )
        config = json.loads(json_str)
        assert "mcpServers" in config
        srv = config["mcpServers"]["kathara_base_mcp_server"]
        assert srv["type"] == "stdio"
        assert srv["command"] == "python3"
        assert srv["args"] == ["/path/base.py"]
        assert srv["env"]["SESSION"] == "sess-abc"

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
                llm_provider="anthropic",
            )

    @pytest.mark.parametrize(
        ("phase", "expected_servers"),
        [
            ("diagnosis", {"kathara_base_mcp_server"}),
            ("submission", {"task_mcp_server"}),
        ],
    )
    def test_mcp_config_exposes_only_current_phase_servers(
        self, tmp_path, agent_task, phase: str, expected_servers: set[str]
    ) -> None:
        agent_task()
        worker = ClaudeWorker(
            session_id="sess-123",
            session_dir=str(tmp_path),
            phase=phase,
            model="test-model",
            llm_provider="anthropic",
        )
        worker.workspace.mkdir()
        worker._write_mcp_config()

        config = json.loads(worker._mcp_config_path.read_text())
        assert set(config["mcpServers"]) == expected_servers
