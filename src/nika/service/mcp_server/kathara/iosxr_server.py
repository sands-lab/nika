from mcp.server.fastmcp import FastMCP

from nika.service.kathara import KatharaIOSXRAPI
from nika.service.mcp_server.session_context import get_lab_name
from nika.utils.errors import safe_tool

# Initialize FastMCP server
mcp = FastMCP("kathara_iosxr_mcp_server")


@safe_tool
@mcp.tool()
def iosxr_get_bgp_conf(router_name: str) -> str:
    """Get the BGP configuration from the IOS-XR router.

    Args:
        router_name (str): The name of the router.

    Returns:
        str: The BGP configuration from the IOS-XR router.
    """
    kathara_api = KatharaIOSXRAPI(lab_name=get_lab_name())
    return kathara_api.iosxr_get_bgp_conf(router_name)


@safe_tool
@mcp.tool()
def iosxr_show_running_config(router_name: str) -> str:
    """Get the running configuration from the IOS-XR router.

    Args:
        router_name (str): The name of the router.
    Returns:
        str: The running configuration from the IOS-XR router.
    """
    kathara_api = KatharaIOSXRAPI(lab_name=get_lab_name())
    return kathara_api.iosxr_show_running_config(router_name)


@safe_tool
@mcp.tool()
def iosxr_show_route(router_name: str) -> str:
    """Get the IP routing table from the IOS-XR router.

    Args:
        router_name (str): The name of the router.
    Returns:
        str: The IP routing table from the IOS-XR router.
    """
    kathara_api = KatharaIOSXRAPI(lab_name=get_lab_name())
    return kathara_api.iosxr_show_route(router_name)


@safe_tool
@mcp.tool()
def iosxr_exec(router_name: str, command: str) -> str:
    """Execute an xr_cli command on an IOS-XR router."""
    kathara_api = KatharaIOSXRAPI(lab_name=get_lab_name())
    return kathara_api.iosxr_exec(router_name, command)


if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport="stdio")
