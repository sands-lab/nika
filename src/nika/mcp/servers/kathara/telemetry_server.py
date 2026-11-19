import json
from typing import Literal

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
    view: Literal["traces", "delivery"] = "traces",
    egress_switch: str | None = None,
    egress_port: int | None = None,
    downstream_switch: str | None = None,
) -> dict:
    """Return a bounded page of INT-MD traces, newest page first.

    The collector decodes P4.org v2.0 reports with v2.1 INT-MD metadata. Each
    trace contains the observed path, hop_latency in BMv2 microseconds, and
    queue_occupancy in packets at dequeue. packet_timestamp is collector receipt
    time in Unix nanoseconds. Prometheus exposes the latest measurements per
    switch/egress port; this tool reads collector-side per-flow trace storage.

    Follow next_offset with the same filters and a fixed end_time to read older traces.
    Use delivery view to count mature packet traces observed at an egress.
    Missing counts exclude the in-flight grace period. Queue measurements accompany the counts.
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
        **(
            {"egress_switch": egress_switch, "egress_port": egress_port}
            if egress_switch is not None or egress_port is not None
            else {}
        ),
    )

    if view == "delivery":
        selected = [
            hop
            for row in rows
            for hop in row.get("hop_sequence", [])
            if (egress_switch is None or hop.get("switch") == egress_switch)
            and (egress_port is None or hop.get("egress_port") == egress_port)
        ]

        def crossed(row):
            if downstream_switch is not None:
                return downstream_switch in row.get("arrival_switches", []) or any(
                    hop.get("switch") == downstream_switch
                    for hop in row.get("hop_sequence", [])
                )
            return bool(row.get("sink_seen"))

        delivered = sum(crossed(row) for row in rows)
        return {
            "observed": len(rows),
            "delivered": delivered,
            "missing": len(rows) - delivered,
            "max_queue_occupancy": max(
                (hop["queue_occupancy"] for hop in selected), default=None
            ),
            "egress_switch": egress_switch,
            "egress_port": egress_port,
            "downstream_switch": downstream_switch,
            "offset": offset,
            "next_offset": offset + len(rows) if rows else None,
        }
    page = {"records": rows, "offset": offset, "next_offset": None}
    while rows and len(json.dumps(page, indent=2)) > 7000:
        if len(rows) == 1:
            return {
                "error": "Trace exceeds the output budget; narrow the flow filters or use delivery view",
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
