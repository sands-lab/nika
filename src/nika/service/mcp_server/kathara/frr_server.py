from mcp.server.fastmcp import FastMCP

from nika.service.kathara import KatharaFRRAPI
from nika.service.mcp_server.session_context import get_lab_name, get_session_meta
from nika.utils.errors import safe_tool

# Initialize FastMCP server
mcp = FastMCP("kathara_frr_mcp_server")


def _api() -> KatharaFRRAPI:
    return KatharaFRRAPI(lab_name=get_lab_name(), session_meta=get_session_meta())


@safe_tool
@mcp.tool()
def frr_get_bgp_conf(router_name: str) -> str:
    """Get the BGP configuration from the FRR router.

    Args:
        router_name (str): The name of the router.

    Returns:
        str: The BGP configuration from the FRR router.
    """
    return _api().frr_get_bgp_conf(router_name)


@safe_tool
@mcp.tool()
def frr_show_running_config(router_name: str) -> str:
    """Get the running configuration from the FRR router.

    Args:
        router_name (str): The name of the router.
    Returns:
        str: The running configuration from the FRR router.
    """
    return _api().frr_show_running_config(router_name)


@safe_tool
@mcp.tool()
def frr_show_ip_route(router_name: str) -> str:
    """Get the IP routing table from the FRR router.

    Args:
        router_name (str): The name of the router.
    Returns:
        str: The IP routing table from the FRR router.
    """
    return _api().frr_show_route(router_name)


@safe_tool
@mcp.tool()
def frr_get_ospf_conf(router_name: str) -> str:
    """Get the OSPF configuration from the FRR router.

    Args:
        router_name (str): The name of the router.

    Returns:
        str: The OSPF configuration from the FRR router.
    """
    return _api().frr_get_ospf_conf(router_name)


@safe_tool
@mcp.tool()
def frr_exec(router_name: str, command: str) -> str:
    """Execute a vtysh command on a FRR router."""
    return _api().frr_exec(router_name, command)


@safe_tool
@mcp.tool()
def frr_get_routing_state(
    device: str,
    neighbor: str | None = None,
    prefix: str | None = None,
) -> str:
    """Get aggregated BGP routing state from an FRR device.

    Returns BGP summary, neighbor detail (session state, received/accepted
    prefixes, configured maximum-prefix, last reset reason when present), and
    RIB entries. Optionally filter to one neighbor and/or one prefix.

    Args:
        device: Router name.
        neighbor: Optional BGP neighbor address for focused neighbor stats.
        prefix: Optional prefix filter for the BGP RIB section.
    """
    return _api().frr_get_routing_state(device, neighbor=neighbor, prefix=prefix)


@safe_tool
@mcp.tool()
def frr_get_rpki_status(device: str, prefix: str | None = None) -> str:
    """Get RPKI/RTR status and optional per-prefix validation detail.

    Without prefix: cache connection and validation summary.
    With prefix: VRP / origin ASN / validation state when available.
    """
    return _api().frr_get_rpki_status(device, prefix=prefix)


if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport="stdio")
