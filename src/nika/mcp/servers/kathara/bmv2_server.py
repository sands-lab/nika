"""MCP tools for live BMv2 / P4Runtime access."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_name
from nika.service.kathara import KatharaBMv2API

mcp = FastMCP("kathara_bmv2_mcp_server")


@mcp.tool()
def p4rt_exec(args: str) -> str:
    """Run p4rt_manager.py on fabric_mgr with the given CLI arguments.

    Example args: ``read``, ``read --switch leaf_1``.
    """
    return KatharaBMv2API(lab_name=get_lab_name()).p4rt_exec(args)


if __name__ == "__main__":
    mcp.run(transport="stdio")
