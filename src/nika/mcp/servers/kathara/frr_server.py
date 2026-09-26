from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_name, get_session_meta
from nika.service.kathara import KatharaFRRAPI

mcp = FastMCP("kathara_frr_mcp_server")


def _api() -> KatharaFRRAPI:
    return KatharaFRRAPI(lab_name=get_lab_name(), session_meta=get_session_meta())


@mcp.tool()
def frr_get_bgp_conf(router_name: str) -> str:
    """Get BGP configuration from an FRR router."""
    return _api().frr_get_bgp_conf(router_name)


@mcp.tool()
def frr_show_running_config(router_name: str) -> str:
    """Get running configuration from an FRR router."""
    return _api().frr_show_running_config(router_name)


@mcp.tool()
def frr_show_ip_route(router_name: str) -> str:
    """Get the IP routing table from an FRR router."""
    return _api().frr_show_route(router_name)


@mcp.tool()
def frr_get_ospf_conf(router_name: str) -> str:
    """Get OSPF configuration from an FRR router."""
    return _api().frr_get_ospf_conf(router_name)


@mcp.tool()
def frr_exec(router_name: str, command: str) -> str:
    """Execute a vtysh command on an FRR router."""
    return _api().frr_exec(router_name, command)


@mcp.tool()
def frr_get_rpki_status(device: str, prefix: str | None = None) -> str:
    """Get RPKI/RTR status and optional per-prefix validation detail.

    Without prefix: cache connection and validation summary.
    With prefix: VRP / origin ASN / validation state when available.
    """
    return _api().frr_get_rpki_status(device, prefix=prefix)


if __name__ == "__main__":
    mcp.run(transport="stdio")
