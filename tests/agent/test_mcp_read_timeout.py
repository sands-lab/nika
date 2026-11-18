"""MCP read-timeout wiring for Autogen and mcp-agent clients."""

from __future__ import annotations

import pytest

from agent.byo.autogen.config import to_mcp_params
from agent.byo.mcp_agent.config import _to_server_settings
from agent.utils.mcp_client import mcp_read_timeout_seconds

pytestmark = pytest.mark.unit


def test_mcp_read_timeout_seconds_disabled(agent_task) -> None:
    agent_task(mcp_read_timeout_sec=0)
    assert mcp_read_timeout_seconds() is None


def test_autogen_stdio_params_include_read_timeout(agent_task) -> None:
    agent_task(mcp_read_timeout_sec=90)
    params = to_mcp_params(
        {
            "transport": "stdio",
            "command": "python",
            "args": ["-m", "demo"],
            "env": {},
        }
    )
    assert params.read_timeout_seconds == 90.0


def test_autogen_http_params_include_timeout(agent_task) -> None:
    agent_task(mcp_read_timeout_sec=45)
    params = to_mcp_params(
        {
            "transport": "http",
            "url": "http://127.0.0.1:9/mcp",
            "headers": {"X-Test": "1"},
        }
    )
    assert params.timeout == 45.0
    assert params.sse_read_timeout == 45.0


def test_mcp_agent_settings_include_read_timeout(agent_task) -> None:
    agent_task(mcp_read_timeout_sec=75)
    http = _to_server_settings(
        {
            "transport": "http",
            "url": "http://127.0.0.1:9/mcp",
            "headers": {},
        }
    )
    assert http.read_timeout_seconds == 75
    assert http.http_timeout_seconds == 75
    stdio = _to_server_settings(
        {
            "transport": "stdio",
            "command": "python",
            "args": [],
            "env": {},
        }
    )
    assert stdio.read_timeout_seconds == 75
