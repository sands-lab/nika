from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_name
from nika.service.kathara import KatharaIOSXRAPI
from nika.utils.errors import safe_tool

mcp = FastMCP("kathara_iosxr_mcp_server")


def _api() -> KatharaIOSXRAPI:
    return KatharaIOSXRAPI(lab_name=get_lab_name())


@safe_tool
@mcp.tool()
def iosxr_get_bgp_conf(router_name: str) -> str:
    """Get BGP configuration from an IOS-XR router."""
    return _api().iosxr_get_bgp_conf(router_name)


@safe_tool
@mcp.tool()
def iosxr_show_running_config(router_name: str) -> str:
    """Get running configuration from an IOS-XR router."""
    return _api().iosxr_show_running_config(router_name)


@safe_tool
@mcp.tool()
def iosxr_show_route(router_name: str) -> str:
    """Get the IP routing table from an IOS-XR router."""
    return _api().iosxr_show_route(router_name)


@safe_tool
@mcp.tool()
def iosxr_exec(router_name: str, command: str) -> str:
    """Execute an xr_cli command on an IOS-XR router."""
    return _api().iosxr_exec(router_name, command)


if __name__ == "__main__":
    mcp.run(transport="stdio")
