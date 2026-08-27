from mcp.server.fastmcp import FastMCP

from nika.service.kathara import KatharaRouterOSAPI
from nika.service.mcp_server.session_context import get_lab_name
from nika.utils.errors import safe_tool

# Initialize FastMCP server
mcp = FastMCP("kathara_routeros_mcp_server")


@safe_tool
@mcp.tool()
def routeros_get_bgp_conf(router_name: str) -> str:
    """Get the BGP connection configuration from the RouterOS router.

    Args:
        router_name (str): The name of the router.

    Returns:
        str: The BGP connection configuration from the RouterOS router.
    """
    kathara_api = KatharaRouterOSAPI(lab_name=get_lab_name())
    return kathara_api.routeros_get_bgp_conf(router_name)


@safe_tool
@mcp.tool()
def routeros_show_interfaces(router_name: str) -> str:
    """Get the interface list from the RouterOS router.

    Args:
        router_name (str): The name of the router.
    Returns:
        str: The interface list from the RouterOS router.
    """
    kathara_api = KatharaRouterOSAPI(lab_name=get_lab_name())
    return kathara_api.routeros_show_interfaces(router_name)


@safe_tool
@mcp.tool()
def routeros_show_route(router_name: str) -> str:
    """Get the IP routing table from the RouterOS router.

    Args:
        router_name (str): The name of the router.
    Returns:
        str: The IP routing table from the RouterOS router.
    """
    kathara_api = KatharaRouterOSAPI(lab_name=get_lab_name())
    return kathara_api.routeros_show_route(router_name)


@safe_tool
@mcp.tool()
def routeros_exec(router_name: str, command: str) -> str:
    """Execute a RouterOS CLI command on a RouterOS router."""
    kathara_api = KatharaRouterOSAPI(lab_name=get_lab_name())
    return kathara_api.routeros_exec(router_name, command)


if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport="stdio")
