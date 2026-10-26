from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_api
from nika.service.kathara.netflow_api import netflow_query_for

mcp = FastMCP("kathara_netflow_mcp_server")


@mcp.tool()
def netflow_query(
    start_time: str,
    end_time: str | None = None,
    exporter: str | None = None,
    filter: str | None = None,
    aggregate_by: list[str] | None = None,
    order_by: str = "bytes",
    limit: int = 25,
) -> list[dict]:
    """Query IPFIX flow records exported by the lab devices.

    Each exporting device reports the flows it receives on its data-plane
    interfaces, so a flow appears once per device on its path; ``exporter``
    is that device. ``in_if`` is the kernel ifindex on pmacct exporters
    (map it with ``ip -o link``), the OpenFlow port on OVS exporters
    (map it with ``ovs-ofctl dump-ports-desc``), the sFlow ifIndex on
    SR Linux exporters, and the RouterOS interface index on traffic-flow
    exporters. Records become queryable about 20 seconds after the traffic.

    Args:
        start_time: Window start: ISO-8601, Unix seconds, or ``-N`` for N
            seconds ago (e.g. ``-600``).
        end_time: Window end in the same formats; defaults to now.
        exporter: Only flows exported by this device.
        filter: nfdump filter expression, e.g. ``proto udp and dst ip 10.254.0.6``.
        aggregate_by: Sum counters per key; any of ``router``, ``inif``,
            ``proto``, ``srcip``, ``srcport``, ``dstip``, ``dstport``.
        order_by: ``bytes``, ``packets``, or ``flows`` (descending).
        limit: Maximum rows to return.
    """
    return netflow_query_for(
        get_lab_api(),
        start_time=start_time,
        end_time=end_time,
        exporter=exporter,
        filter=filter,
        aggregate_by=aggregate_by,
        order_by=order_by,
        limit=limit,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
