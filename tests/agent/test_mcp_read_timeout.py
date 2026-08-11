"""MCP read-timeout wiring for Autogen and mcp-agent clients."""

from __future__ import annotations

from agent.byo.autogen.config import to_mcp_params
from agent.byo.mcp_agent.config import _to_server_settings
from agent.utils.mcp_servers import (
    DEFAULT_MCP_READ_TIMEOUT_SECONDS,
    mcp_read_timeout_seconds,
)


def test_mcp_read_timeout_seconds_default(monkeypatch) -> None:
    monkeypatch.delenv("NIKA_MCP_READ_TIMEOUT", raising=False)
    assert mcp_read_timeout_seconds() == DEFAULT_MCP_READ_TIMEOUT_SECONDS


def test_mcp_read_timeout_seconds_disabled(monkeypatch) -> None:
    monkeypatch.setenv("NIKA_MCP_READ_TIMEOUT", "0")
    assert mcp_read_timeout_seconds() is None


def test_autogen_stdio_params_include_read_timeout(monkeypatch) -> None:
    monkeypatch.setenv("NIKA_MCP_READ_TIMEOUT", "90")
    params = to_mcp_params(
        {
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "demo"],
            "env": {},
        }
    )
    assert params.read_timeout_seconds == 90.0


def test_autogen_http_params_include_timeout(monkeypatch) -> None:
    monkeypatch.setenv("NIKA_MCP_READ_TIMEOUT", "45")
    params = to_mcp_params(
        {
            "transport": "http",
            "url": "http://127.0.0.1:9/mcp",
            "headers": {"X-Test": "1"},
        }
    )
    assert params.timeout == 45.0
    assert params.sse_read_timeout == 45.0


def test_mcp_agent_settings_include_read_timeout(monkeypatch) -> None:
    monkeypatch.setenv("NIKA_MCP_READ_TIMEOUT", "75")
    settings = _to_server_settings(
        {
            "transport": "http",
            "url": "http://127.0.0.1:9/mcp",
            "headers": {},
        }
    )
    assert settings.read_timeout_seconds == 75
    assert settings.http_timeout_seconds == 75


def test_mcp_agent_stdio_settings_include_read_timeout(monkeypatch) -> None:
    monkeypatch.setenv("NIKA_MCP_READ_TIMEOUT", "60")
    settings = _to_server_settings(
        {
            "transport": "stdio",
            "command": "python",
            "args": [],
            "env": {},
        }
    )
    assert settings.read_timeout_seconds == 60
