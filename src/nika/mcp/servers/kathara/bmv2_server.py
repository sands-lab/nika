"""MCP tools for live BMv2 / P4Runtime access."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_name
from nika.service.kathara import KatharaBMv2API

mcp = FastMCP("kathara_bmv2_mcp_server")


@mcp.tool()
def p4rt_exec(args: str) -> str:
    """Query live P4Runtime state through the fabric controller (read-only).

    Commands: ``read [--switch NAME]``, ``read-table --switch NAME --table T``,
    ``counters [--switch NAME]``; ``--help`` lists them.
    Example args: ``read``, ``read --switch leaf_1``.
    """
    return KatharaBMv2API(lab_name=get_lab_name()).p4rt_exec(args)


if __name__ == "__main__":
    mcp.run(transport="stdio")
