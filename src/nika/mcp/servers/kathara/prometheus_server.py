from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_name, get_session_meta
from nika.service.kathara import KatharaPrometheusAPI

mcp = FastMCP("kathara_prometheus_mcp_server")


@mcp.tool()
def prometheus_query(
    query: str,
    start_time: str | None = None,
    end_time: str | None = None,
    step: str = "15s",
    limit: int = 50,
) -> list[dict]:
    """Query device metrics scraped by Prometheus, in PromQL.

    Each exporting device is scraped every 5 seconds and labelled ``node``;
    ``device`` is an interface name. Metrics come from node_exporter and cover
    the device's network stack: interface counters and state
    (``node_network_*``), protocol statistics (``node_netstat_*``), sockets
    (``node_sockstat_*``), conntrack (``node_nf_conntrack_*``), and ARP
    (``node_arp_*``). ``up`` shows whether a device answers. For example,
    ``rate(node_network_receive_bytes_total{node="chinng"}[30s])`` or
    ``node_network_up == 0``.

    Args:
        query: PromQL expression.
        start_time: Range start: ISO-8601, Unix seconds, or ``-N`` for N seconds
            ago (e.g. ``-600``). Omit it for an instant query.
        end_time: Range end in the same formats (defaults to now), or the time
            of an instant query.
        step: Range resolution, e.g. ``15s`` or ``1m``.
        limit: Maximum series to return.
    """
    return KatharaPrometheusAPI(
        lab_name=get_lab_name(), session_meta=get_session_meta()
    ).prometheus_query(
        query=query,
        start_time=start_time,
        end_time=end_time,
        step=step,
        limit=limit,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
