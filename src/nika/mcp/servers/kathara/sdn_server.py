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


@safe_tool
@mcp.tool()
def sdn_ovs_exec(switch_name: str, command: str) -> str:
    """Run an OVS/OpenFlow CLI command on a switch.

    Typical commands: ``ovs-ofctl -O OpenFlow13 dump-flows <switch>``,
    ``ovs-vsctl show``, ``ovs-vsctl get-controller <switch>``.
    """
    return _json(_api().sdn_ovs_exec(switch_name, command))


@safe_tool
@mcp.tool()
def sdn_controller_logs(rows: int = 80) -> str:
    """Return recent ONOS/karaf controller log lines (1-500; default 80)."""
    return _json(_api().sdn_controller_logs(rows=rows))


if __name__ == "__main__":
    mcp.run(transport="stdio")
