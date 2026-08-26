from mcp.server.fastmcp import FastMCP

from nika.service.kathara import KatharaTelemetryAPI
from nika.service.mcp_server.session_context import get_lab_name
from nika.utils.errors import safe_tool

mcp = FastMCP("kathara_telemetry_mcp_server")


@safe_tool
@mcp.tool()
def int_query_telemetry(
    start_time: str,
    end_time: str | None = None,
    src: str | None = None,
    dst: str | None = None,
    protocol: str | None = None,
    src_port: int | None = None,
    dst_port: int | None = None,
    flow_id: str | None = None,
    packet_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Return observed INT-MX packet traces and hop metadata."""
    return KatharaTelemetryAPI(lab_name=get_lab_name()).int_query_telemetry(
        start_time=start_time,
        end_time=end_time,
        src=src,
        dst=dst,
        protocol=protocol,
        src_port=src_port,
        dst_port=dst_port,
        flow_id=flow_id,
        packet_id=packet_id,
        limit=limit,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
