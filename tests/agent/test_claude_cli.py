from __future__ import annotations
import pytest
import json
import os
import unittest.mock
from agent.cli.claude.claude_worker import ClaudeWorker, _build_mcp_json
from agent.cli.claude.config import (
    has_env_claude_credentials,
    prepare_claude_subprocess_env,
    use_bare_claude_mode,
)
from tests.support.integration_pipeline import load_test_env

pytestmark = pytest.mark.unit

load_test_env()


class ClaudeConfigTest:
    """Claude env model and auth helpers."""

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
        self, tmp_path, phase: str, expected_servers: set[str]
    ) -> None:
        worker = ClaudeWorker(
            session_id="sess-123",
            session_dir=str(tmp_path),
            phase=phase,
            model="test-model",
            llm_provider="anthropic",
        )
        worker.workspace.mkdir()
        servers = {
            "kathara_base_mcp_server": {"transport": "http", "url": "http://base"},
            "task_mcp_server": {"transport": "http", "url": "http://task"},
        }
        with (
            unittest.mock.patch(
                "agent.utils.mcp_client._load_session_mcp_config",
                return_value=servers,
            ),
        ):
            worker._write_mcp_config()

        config = json.loads(worker._mcp_config_path.read_text())
        assert set(config["mcpServers"]) == expected_servers
