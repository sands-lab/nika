from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_srl_api

mcp = FastMCP("containerlab_srl_mcp_server")


@mcp.tool()
def srl_exec_cli(device_name: str, command: str) -> str:
    """Execute an ``sr_cli`` command on an SR Linux router in the lab."""
    return get_srl_api().srl_exec_cli(device_name, command)


@mcp.tool()
def srl_get_bgp_as(device_name: str) -> int:
    """Return the BGP autonomous system number configured on an SR Linux router."""
    return get_srl_api().srl_get_bgp_as(device_name)


@mcp.tool()
def srl_show_running_config(device_name: str) -> str:
    """Get the running configuration from an SR Linux router."""
    return get_srl_api().srl_exec_cli(device_name, "info from running")


@mcp.tool()
def srl_show_bgp_summary(device_name: str) -> str:
    """Get BGP summary from an SR Linux router."""
    return get_srl_api().srl_exec_cli(
        device_name,
        "show network-instance default protocols bgp summary",
    )


@mcp.tool()
def srl_show_ip_route(device_name: str) -> str:
    """Get the IPv4 routing table from an SR Linux router."""
    return get_srl_api().srl_exec_cli(
        device_name,
        "show network-instance default route-table ipv4-unicast summary",
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
