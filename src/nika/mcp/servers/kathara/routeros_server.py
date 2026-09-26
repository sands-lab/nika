from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_name
from nika.service.kathara import KatharaRouterOSAPI
from nika.utils.errors import safe_tool

mcp = FastMCP("kathara_routeros_mcp_server")


def _api() -> KatharaRouterOSAPI:
    return KatharaRouterOSAPI(lab_name=get_lab_name())


@safe_tool
@mcp.tool()
def routeros_exec(router_name: str, command: str) -> str:
    """Execute a RouterOS CLI command on a RouterOS router."""
    return _api().routeros_exec(router_name, command)


if __name__ == "__main__":
    mcp.run(transport="stdio")
