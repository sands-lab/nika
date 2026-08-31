"""Host-side MCP config serialization for sandbox manifests."""

from __future__ import annotations

from agent.utils.mcp_servers import select_session_servers, session_http_headers
from nika.mcp.registry import MCP_SERVER_SPECS


def build_sandbox_mcp_servers(
    *,
    session_id: str,
    scenario_name: str,
    backend: str | None,
    gateway_agent_url: str,
) -> dict:
    """Build HTTP MCP client entries reachable from inside an sbx microVM."""
    base = gateway_agent_url.rstrip("/")
    server_names = select_session_servers(scenario_name, backend=backend)
    headers = session_http_headers(session_id)
    return {
        name: {
            "transport": "http",
            "url": f"{base}/mcp/{name}/mcp",
            "headers": headers,
        }
        for name in server_names
        if name in MCP_SERVER_SPECS
    }
