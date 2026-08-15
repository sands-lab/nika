"""MCP read-timeout wiring for Autogen and mcp-agent clients."""

from __future__ import annotations

from agent.byo.autogen.config import to_mcp_params
from agent.byo.mcp_agent.config import _to_server_settings
from agent.utils.mcp_servers import (
    DEFAULT_MCP_READ_TIMEOUT_SECONDS,
    mcp_read_timeout_seconds,
)
from nika.run_config.loader import reset_run_config, set_run_config
from nika.run_config.schema import RunConfig


def _set_mcp_timeout(seconds: float) -> None:
    set_run_config(
        RunConfig.model_validate({"nika": {"mcp": {"read_timeout_sec": seconds}}})
    )


def test_mcp_read_timeout_seconds_default() -> None:
    reset_run_config()
    assert mcp_read_timeout_seconds() == DEFAULT_MCP_READ_TIMEOUT_SECONDS


def test_mcp_read_timeout_seconds_disabled() -> None:
    try:
        _set_mcp_timeout(0)
        assert mcp_read_timeout_seconds() is None
    finally:
        reset_run_config()


def test_autogen_stdio_params_include_read_timeout() -> None:
    try:
        _set_mcp_timeout(90)
        params = to_mcp_params(
            {
                "transport": "stdio",
                "command": "python",
                "args": ["-m", "demo"],
                "env": {},
            }
        )
        assert params.read_timeout_seconds == 90.0
    finally:
        reset_run_config()


def test_autogen_http_params_include_timeout() -> None:
    try:
        _set_mcp_timeout(45)
        params = to_mcp_params(
            {
                "transport": "http",
                "url": "http://127.0.0.1:9/mcp",
                "headers": {"X-Test": "1"},
            }
        )
        assert params.timeout == 45.0
        assert params.sse_read_timeout == 45.0
    finally:
        reset_run_config()


def test_mcp_agent_settings_include_read_timeout() -> None:
    try:
        _set_mcp_timeout(75)
        settings = _to_server_settings(
            {
                "transport": "http",
                "url": "http://127.0.0.1:9/mcp",
                "headers": {},
            }
        )
        assert settings.read_timeout_seconds == 75
        assert settings.http_timeout_seconds == 75
    finally:
        reset_run_config()


def test_mcp_agent_stdio_settings_include_read_timeout() -> None:
    try:
        _set_mcp_timeout(60)
        settings = _to_server_settings(
            {
                "transport": "stdio",
                "command": "python",
                "args": [],
                "env": {},
            }
        )
        assert settings.read_timeout_seconds == 60
    finally:
        reset_run_config()
