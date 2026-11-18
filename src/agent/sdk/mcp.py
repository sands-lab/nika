"""Shared MCP config adapters for SDK-based agents."""

from __future__ import annotations

import sys
from typing import Any


def _resolve_python() -> str:
    """Interpreter used to spawn stdio MCP servers."""
    return sys.executable or "python3"


def to_sdk_mcp_servers(config: dict[str, Any]) -> dict[str, Any]:
    """Adapt the session MCP client config to claude-agent-sdk format."""
    servers: dict[str, Any] = {}
    for name, spec in config.items():
        transport = spec.get("transport", "stdio")
        if transport == "http":
            servers[name] = {
                "type": "http",
                "url": spec["url"],
                "headers": dict(spec.get("headers") or {}),
            }
            continue

        command = spec.get("command")
        if command in ("python3", "python"):
            command = _resolve_python()
        servers[name] = {
            "type": "stdio",
            "command": command,
            "args": list(spec.get("args", [])),
            "env": dict(spec.get("env", {})),
        }
    return servers
