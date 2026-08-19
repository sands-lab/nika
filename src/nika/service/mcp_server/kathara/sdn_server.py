"""MCP tools for ONOS + OVS Clos fabric evidence (sdn_l3_clos)."""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from nika.service.kathara import KatharaSdnAPI
from nika.service.mcp_server.session_context import get_lab_name, get_session_meta
from nika.utils.errors import safe_tool

mcp = FastMCP("kathara_sdn_mcp_server")


def _api() -> KatharaSdnAPI:
    return KatharaSdnAPI(lab_name=get_lab_name())


def _topo_size() -> str:
    meta = get_session_meta()
    size = meta.get("scenario_topo_size") or (meta.get("scenario_params") or {}).get(
        "topo_size"
    )
    return size if size in ("s", "m", "l") else "s"


def _json(payload: object) -> str:
    return json.dumps(payload, indent=2, default=str)


@safe_tool
@mcp.tool()
def sdn_get_fabric_state(
    switch_name: str | None = None,
    source: str | None = None,
    target_ip: str | None = None,
    log_rows: int = 0,
) -> str:
    """Get aggregated SDN fabric evidence (like frr_get_routing_state).

    Returns ONOS topology (devices/links/hosts), controller application state,
    live ONOS flow/group store, and switch-observed OpenFlow flows/groups/port
    counters/OVS status. Optional reachability probe and controller log tail.
    Live controller state and observed dataplane are separate sections; this
    tool does not assert mismatches or name faults.

    Args:
        switch_name: Optional leaf/spine to focus dataplane dumps and filter
            live ONOS flows/groups to that device (e.g. leaf_1). Default
            samples the first few switches.
        source: Optional endpoint name for a ping probe (requires target_ip).
        target_ip: Optional IPv4 destination for the ping probe.
        log_rows: If >0, include trailing ONOS/karaf log lines (max 500).
    """
    return _json(
        _api().sdn_get_fabric_state(
            topo_size=_topo_size(),  # type: ignore[arg-type]
            switch_name=switch_name,
            source=source,
            target_ip=target_ip,
            log_rows=log_rows,
        )
    )


@safe_tool
@mcp.tool()
def sdn_controller_logs(rows: int = 80) -> str:
    """Return recent ONOS/karaf controller log lines.

    Args:
        rows: Number of trailing log lines (1-500). Defaults to 80.
    """
    return _json(_api().sdn_controller_logs(rows=rows))


@safe_tool
@mcp.tool()
def sdn_endpoint_reachability(source: str, target_ip: str, count: int = 3) -> str:
    """Ping from an endpoint to a target IP and return raw ping output.

    Does not classify the result as a fault.

    Args:
        source: Endpoint machine name (e.g. client_1_1).
        target_ip: Destination IPv4 address.
        count: ICMP echo count (1-10). Defaults to 3.
    """
    return _json(_api().sdn_endpoint_reachability(source, target_ip, count=count))


if __name__ == "__main__":
    mcp.run(transport="stdio")
