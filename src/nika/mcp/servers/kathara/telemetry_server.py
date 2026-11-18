import json

from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_name
from nika.service.kathara import KatharaTelemetryAPI

mcp = FastMCP("kathara_telemetry_mcp_server")


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
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Return a bounded page of INT-MX traces, newest page first.

    Follow next_offset with the same filters and a fixed end_time to read older traces.
    Inspect oversized traces on the collector with exec_shell and select the required hop fields.
    """
    rows = KatharaTelemetryAPI(lab_name=get_lab_name()).int_query_telemetry(
        start_time=start_time,
        end_time=end_time,
        src=src,
        dst=dst,
        protocol=protocol,
        src_port=src_port,
        dst_port=dst_port,
        flow_id=flow_id,
        packet_id=packet_id,
        limit=min(max(limit, 1), 1000),
        offset=offset,
    )

    page = {"records": rows, "offset": offset, "next_offset": None}
    while rows and len(json.dumps(page, indent=2)) > 7000:
        if len(rows) == 1:
            return {
                "error": "Trace exceeds the output budget; use exec_shell on the collector to inspect selected hop fields",
                "trace": {
                    key: rows[0].get(key)
                    for key in (
                        "flow_id",
                        "packet_id",
                        "src",
                        "dst",
                        "sink_seen",
                        "trace_complete",
                    )
                },
                "hop_count": len(rows[0].get("hop_sequence", [])),
                "offset": offset,
            }
        rows.pop(0)
    if rows:
        page["next_offset"] = offset + len(rows)
    return page


if __name__ == "__main__":
    mcp.run(transport="stdio")
