"""Phase-based MCP server access policy."""

from __future__ import annotations

from agent.protocols import DIAGNOSIS, SUBMISSION
from nika.service.mcp_gateway.session_registry import get_session
from nika.service.mcp_server.registry import MCP_SERVER_SPECS, SUBMISSION_SERVER


def server_phase_role(server_name: str) -> str:
    spec = MCP_SERVER_SPECS[server_name]
    return "submission" if spec.role == "task" else "diagnosis"


def is_server_allowed(session_id: str, server_name: str) -> bool:
    """Return whether *server_name* may be accessed for *session_id*."""
    if server_name not in MCP_SERVER_SPECS:
        return False

    entry = get_session(session_id)
    if entry is None:
        return False

    role = server_phase_role(server_name)
    if entry.phase == DIAGNOSIS:
        return role == "diagnosis"
    if entry.phase == SUBMISSION:
        return server_name == SUBMISSION_SERVER
    return False
