from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from nika.service.kathara import KatharaBMv2API
from nika.service.mcp_server.session_context import get_lab_name
from nika.utils.errors import safe_tool

mcp = FastMCP("kathara_bmv2_mcp_server")


@safe_tool
@mcp.tool()
def p4_get_runtime_state(switch_name: str | None = None) -> str:
    """Read live pipeline, forwarding, counters, queues, flow, and ECN state."""
    payload = KatharaBMv2API(lab_name=get_lab_name()).p4_get_runtime_state(
        switch_name=switch_name
    )
    return json.dumps(payload, indent=2, default=str)


if __name__ == "__main__":
    mcp.run(transport="stdio")
