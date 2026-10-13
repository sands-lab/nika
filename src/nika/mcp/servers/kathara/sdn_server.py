"""MCP tools for ONOS + OVS Clos fabric (sdn_l3_clos)."""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_name
from nika.service.kathara import KatharaSdnAPI
from nika.utils.errors import safe_tool

mcp = FastMCP("kathara_sdn_mcp_server")


def _api() -> KatharaSdnAPI:
    return KatharaSdnAPI(lab_name=get_lab_name())


def _json(payload: object) -> str:
    return json.dumps(payload, indent=2, default=str)


@safe_tool
@mcp.tool()
def sdn_onos_rest(path: str) -> str:
    """GET an ONOS REST API path (e.g. /onos/v1/devices, /onos/v1/flows).

    Runs curl from fabric_mgr against the lab ONOS controller. Returns JSON
    body plus controller addressing metadata.
    """
    return _json(_api().sdn_onos_rest(path))


if __name__ == "__main__":
    mcp.run(transport="stdio")
