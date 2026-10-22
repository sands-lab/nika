"""Session-scoped Kubernetes event MCP server."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from mcp.server.fastmcp import FastMCP

from nika.mcp.k8s.client import get_client
from nika.mcp.tool_output import tool_output_max_chars
from nika.utils.errors import safe_tool

SERVER_NAME = "k8s_mcp_server"
_MESSAGE_CAP = 240

mcp = FastMCP(name=SERVER_NAME, host="127.0.0.1", port=8000, log_level="INFO")


def dump_events_json(
    events: Sequence[Mapping[str, Any]], *, max_chars: int | None = None
) -> str:
    """Serialize events as JSON that stays within the MCP text budget."""
    items = [dict(event) for event in events]
    budget = tool_output_max_chars() if max_chars is None else max_chars

    def encode(*, pretty: bool) -> str:
        if pretty:
            return json.dumps(items, default=str, indent=2)
        return json.dumps(items, default=str, separators=(",", ":"))

    if budget <= 0:
        return encode(pretty=True)
    text = encode(pretty=True)
    if len(text) <= budget:
        return text
    text = encode(pretty=False)
    if len(text) <= budget:
        return text
    for event in items:
        message = str(event.get("message") or "")
        if len(message) > _MESSAGE_CAP:
            event["message"] = message[: _MESSAGE_CAP - 3] + "..."
    text = encode(pretty=False)
    while items and len(text) > budget:
        del items[0]
        text = encode(pretty=False)
    return text


@safe_tool
@mcp.tool()
def k8s_list_events(
    namespace: str = "",
    field_selector: str = "",
    all_namespaces: bool = False,
    limit: int = 100,
) -> str:
    """List Kubernetes events.

    Args:
        namespace: Namespace (ignored when all_namespaces is true).
        field_selector: Event field selector.
        all_namespaces: List cluster-wide events.
        limit: Max events to return.
    """
    return dump_events_json(
        get_client().list_events(
            namespace=namespace or None,
            field_selector=field_selector or None,
            all_namespaces=all_namespaces,
            limit=limit,
        )
    )
