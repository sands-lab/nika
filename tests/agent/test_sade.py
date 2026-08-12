from __future__ import annotations
import pytest
import os
import sys
import unittest.mock
from agent.sdk.mcp import to_sdk_mcp_servers
from agent.community.sade.config import prepare_sade_sdk_env, sade_credentials_available
from tests.support.integration_pipeline import load_test_env

load_test_env()


class SadeConfigTest:
    """Model and credential resolution for community.sade."""

    def test_prepare_env_maps_auth_token_to_api_key(self) -> None:
        with unittest.mock.patch.dict(
            os.environ,
            {
                "ANTHROPIC_AUTH_TOKEN": "tok",
                "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
            },
            clear=True,
        ):
            env = prepare_sade_sdk_env(session_id="sess-abc")
        assert env["ANTHROPIC_API_KEY"] == "tok"
        assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
        assert env["NIKA_SESSION_ID"] == "sess-abc"

    def test_prepare_env_requires_credentials(self) -> None:
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            with pytest.raises(RuntimeError):
                prepare_sade_sdk_env(session_id="sess-abc")

    def test_sade_credentials_available_with_auth_token(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {"ANTHROPIC_AUTH_TOKEN": "tok"}, clear=True
        ):
            assert sade_credentials_available()


class SadeMcpAdapterTest:
    """MCP config adaptation for claude-agent-sdk."""

    def test_converts_transport_to_stdio_type(self) -> None:
        servers = to_sdk_mcp_servers(
            {
                "kathara_base_mcp_server": {
                    "transport": "stdio",
                    "command": "python3",
                    "args": ["/path/base.py"],
                    "env": {"NIKA_SESSION_ID": "sess-abc"},
                }
            }
        )
        srv = servers["kathara_base_mcp_server"]
        assert srv["type"] == "stdio"
        assert srv["command"] == sys.executable
        assert srv["args"] == ["/path/base.py"]
        assert srv["env"]["NIKA_SESSION_ID"] == "sess-abc"

    def test_multiple_servers_all_present(self) -> None:
        servers = to_sdk_mcp_servers(
            {
                "kathara_base_mcp_server": {
                    "command": "python3",
                    "args": ["/path/base.py"],
                },
                "task_mcp_server": {"command": "python3", "args": ["/path/task.py"]},
            }
        )
        assert "kathara_base_mcp_server" in servers
        assert "task_mcp_server" in servers
