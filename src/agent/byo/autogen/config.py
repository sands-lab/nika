"""Bridge the session MCP server config to AutoGen MCP server params."""

from __future__ import annotations

from autogen_ext.tools.mcp import (
    StdioServerParams,
    StreamableHttpServerParams,
)

from agent.utils.mcp_client import mcp_read_timeout_seconds


def to_mcp_params(server: dict) -> StdioServerParams | StreamableHttpServerParams:
    transport = server.get("transport", "stdio")
    read_timeout = mcp_read_timeout_seconds()
    # Autogen Stdio uses float seconds; HTTP uses timeout + sse_read_timeout.
    if transport == "http":
        kwargs: dict = {
            "url": server["url"],
            "headers": dict(server.get("headers") or {}),
        }
        if read_timeout is not None:
            kwargs["timeout"] = read_timeout
            kwargs["sse_read_timeout"] = read_timeout
        return StreamableHttpServerParams(**kwargs)
    kwargs = {
        "command": server["command"],
        "args": server.get("args", []),
        "env": server.get("env"),
    }
    if read_timeout is not None:
        kwargs["read_timeout_seconds"] = float(read_timeout)
    return StdioServerParams(**kwargs)
