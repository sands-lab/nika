from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_api
from nika.service.kathara.prometheus_api import prometheus_query_for

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
    ``device`` is an interface name. Linux nodes export node_exporter series
    for the network stack: interface counters and state (``node_network_*``),
    protocol statistics (``node_netstat_*``), sockets (``node_sockstat_*``),
    conntrack (``node_nf_conntrack_*``), and ARP (``node_arp_*``). OVS
    switches export OpenFlow port counters (``openflow_port_*``). P4 switches
    export pipeline counters (``p4_ingress_*``, ``p4_egress_*``) labelled
    with the BMv2 node. SR Linux nodes export interface counters
    (``srl_interface_*``). IOS XR nodes export ``xr_interface_*``. RouterOS
    nodes export ``routeros_interface_*`` and ``routeros_bgp_peer_up``.
    FRR routers also export ``frr_bgp_peer_*``. ``up`` shows whether a device
    answers. For example,
    ``rate(node_network_receive_bytes_total{node="chinng"}[30s])``,
    ``openflow_port_rx_bytes{node="leaf_1"}``,
    ``p4_ingress_packets{node="spine_1"}``,
    ``srl_interface_in_packets{node="leaf1"}``,
    ``srl_bgp_peer_up{node="leaf1"}``,
    ``xr_interface_in_packets{node="router1"}``,
    ``frr_bgp_peer_up{node="leaf_router_0_0"}``, or
    ``routeros_interface_rx_packets{node="router1"}``.

    Args:
        query: PromQL expression.
        start_time: Range start: ISO-8601, Unix seconds, or ``-N`` for N seconds
            ago (e.g. ``-600``). Omit it for an instant query.
        end_time: Range end in the same formats (defaults to now), or the time
            of an instant query.
        step: Range resolution, e.g. ``15s`` or ``1m``.
        limit: Maximum series to return.
    """
    return prometheus_query_for(
        get_lab_api(),
        query=query,
        start_time=start_time,
        end_time=end_time,
        step=step,
        limit=limit,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
