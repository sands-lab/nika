from __future__ import annotations
import pytest
import os
import sys
import unittest.mock
from agent.sdk.claude_sdk.config import (
    claude_sdk_credentials_available,
    prepare_claude_sdk_env,
    resolve_claude_sdk_model,
)
from agent.sdk.mcp import to_sdk_mcp_servers
from tests.support.integration_pipeline import load_test_env

load_test_env()


class ClaudeSdkConfigTest:
    """Model and credential resolution for sdk.claude_sdk."""

    def test_prepare_env_maps_auth_token_to_api_key(self) -> None:
        with unittest.mock.patch.dict(
            os.environ,
            {
                "ANTHROPIC_AUTH_TOKEN": "tok",
                "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
            },
            clear=True,
        ):
            env = prepare_claude_sdk_env(session_id="sess-abc")
        assert env["ANTHROPIC_API_KEY"] == "tok"
        assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
        assert env["NIKA_SESSION_ID"] == "sess-abc"

    def test_prepare_env_requires_credentials(self) -> None:
        with (
            unittest.mock.patch.dict(os.environ, {}, clear=True),
            unittest.mock.patch(
                "agent.sdk.claude_sdk.config.claude_sbx_secret_available",
                return_value=False,
            ),
            pytest.raises(RuntimeError),
        ):
            prepare_claude_sdk_env(session_id="sess-abc")

    def test_resolve_claude_sdk_model_explicit(self) -> None:
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            assert resolve_claude_sdk_model("custom-model") == "custom-model"


class ClaudeSdkMcpTest:
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

    def test_credentials_available_with_auth_token(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {"ANTHROPIC_AUTH_TOKEN": "tok"}, clear=True
        ):
            assert claude_sdk_credentials_available()
