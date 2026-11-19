from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_srl_api
from nika.utils.errors import safe_tool

mcp = FastMCP("containerlab_srl_mcp_server")


@safe_tool
@mcp.tool()
def srl_exec_cli(device_name: str, command: str) -> str:
    """Execute SR Linux CLI commands on a router.

    Separate configuration session commands with newlines. Include commit now to apply candidate changes.
    """
    return get_srl_api().srl_exec_cli(device_name, command)


if __name__ == "__main__":
    mcp.run(transport="stdio")
