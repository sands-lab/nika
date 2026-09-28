"""Session-scoped Kubernetes event MCP server."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from nika.mcp.k8s.client import get_client
from nika.utils.errors import safe_tool

SERVER_NAME = "k8s_mcp_server"

mcp = FastMCP(name=SERVER_NAME, host="127.0.0.1", port=8000, log_level="INFO")


def _json(payload: Any) -> str:
    return json.dumps(payload, default=str, indent=2)


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
    return _json(
        get_client().list_events(
            namespace=namespace or None,
            field_selector=field_selector or None,
            all_namespaces=all_namespaces,
            limit=limit,
        )
    )
