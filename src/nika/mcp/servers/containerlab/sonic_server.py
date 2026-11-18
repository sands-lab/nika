from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_sonic_api
from nika.utils.errors import safe_tool

mcp = FastMCP("containerlab_sonic_mcp_server")


@safe_tool
@mcp.tool()
def sonic_exec_cli(device_name: str, command: str) -> str:
    """Execute one SONiC CLI command on a SONiC router in the lab.

    The command starts with ``show``, ``config``, ``vtysh``, ``redis-cli`` or
    ``sonic-db-cli``, for example ``show interfaces status`` or
    ``vtysh -c "show bgp summary"``. Shell operators such as pipes are not
    accepted.
    """
    return get_sonic_api().sonic_exec_cli(device_name, command)


if __name__ == "__main__":
    mcp.run(transport="stdio")
