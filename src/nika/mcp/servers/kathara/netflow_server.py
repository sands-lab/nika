from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_api
from nika.service.kathara.netflow_api import flow_api_for

mcp = FastMCP("kathara_netflow_mcp_server")


@mcp.tool()
def flow_coverage(limit: int = 25, offset: int = 0) -> dict:
    """List flow exporters, unsupported nodes, newest record times, and last metric updates."""
    return flow_api_for(get_lab_api()).flow_coverage(limit, offset)


@mcp.tool()
def flow_records(
    start_time: str = "-600",
    end_time: str | None = None,
    exporter: str | None = None,
    filter: str | None = None,
    order_by: str = "tstart",
    limit: int = 25,
) -> dict:
    """Read flow records with healthy interface names, peers, ToS, DSCP, and TCP flags.

    Times accept ISO-8601, Unix seconds, or -N seconds ago. Records lag traffic by about 20 seconds.
    Each exporter observes traffic once per hop. Limit is capped at 1000 rows.
    Filter accepts nfdump syntax. Ordering accepts tstart, tend, duration, bytes, packets, flows, pps, bps, or bpp.
    """
    return flow_api_for(get_lab_api()).flow_records(
        start_time, end_time, exporter, filter, order_by, limit
    )


@mcp.tool()
def flow_top_talkers(
    aggregate_by: list[str],
    start_time: str = "-600",
    end_time: str | None = None,
    exporter: str | None = None,
    filter: str | None = None,
    order_by: str = "bytes",
    limit: int = 25,
) -> dict:
    """Rank flow groups by bytes, packets, flows, pps, bps, or bpp.

    Keys: router, inif, outif, proto, srcip, dstip, srcport, dstport, tos, nexthop, srcas, dstas.
    Subnet keys include srcip4/24 and dstip6/64. Groups retain their exporting device.
    """
    return flow_api_for(get_lab_api()).flow_top_talkers(
        aggregate_by, start_time, end_time, exporter, filter, order_by, limit
    )


@mcp.tool()
def flow_conversations(
    exporter: str,
    start_time: str = "-600",
    end_time: str | None = None,
    filter: str | None = None,
    limit: int = 25,
) -> dict:
    """Read bidirectional conversations from one exporter, with forward and reverse counters."""
    return flow_api_for(get_lab_api()).flow_conversations(
        start_time, end_time, exporter, filter, limit
    )


@mcp.tool()
def flow_timeline(
    start_time: str = "-600",
    end_time: str | None = None,
    metric: str = "bytes",
    split_by: str | None = None,
    step: str = "15s",
    exporter: str | None = None,
    limit: int = 50,
) -> dict:
    """Query flow counter rates per node over time. Split by proto or in_if.

    Metric accepts bytes, packets, or flows. The response includes the executed PromQL query.
    """
    return flow_api_for(get_lab_api()).flow_timeline(
        start_time, end_time, metric, split_by, step, exporter, limit
    )


@mcp.tool()
def flow_current_load(
    metric: str = "bytes",
    exporter: str | None = None,
    average_window: str = "5m",
    limit: int = 50,
) -> dict:
    """Compare the latest flow interval rate with its rolling average and return their ratio."""
    return flow_api_for(get_lab_api()).flow_current_load(
        metric, exporter, average_window, limit
    )


@mcp.tool()
def nfdump(args: list[str]) -> dict:
    """Run a read-only nfdump query over the flow directory, with the exporter address table.

    Pass an argument list. Query flags: -a, -b, -B, -6, -I, -q, -N.
    Value options: -A, -O, -t, -o, -s, -n, -c. Put one filter expression last.
    The service enforces a 25-second timeout and a 100000-character output limit.
    """
    return flow_api_for(get_lab_api()).nfdump(args)


if __name__ == "__main__":
    mcp.run(transport="stdio")
