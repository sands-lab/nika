from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_name, get_session_meta
from nika.service.kathara import KatharaFRRAPI
from nika.utils.errors import safe_tool

mcp = FastMCP("kathara_frr_mcp_server")


def _api() -> KatharaFRRAPI:
    return KatharaFRRAPI(lab_name=get_lab_name(), session_meta=get_session_meta())


@safe_tool
@mcp.tool()
def frr_get_rpki_status(device: str, prefix: str | None = None) -> str:
    """Get RPKI/RTR status and optional per-prefix validation detail.

    Without prefix: cache connection and validation summary.
    With prefix: VRP / origin ASN / validation state when available.
    """
    return _api().frr_get_rpki_status(device, prefix=prefix)


if __name__ == "__main__":
    mcp.run(transport="stdio")
