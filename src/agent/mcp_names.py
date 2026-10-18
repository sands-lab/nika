"""MCP server name constants shared by host and sandbox agent code.

Kept in the ``agent`` package so sandbox microVMs (bundled without ``nika``)
can filter diagnosis vs submission servers.
"""

from __future__ import annotations

SUBMISSION_SERVER = "task_mcp_server"
