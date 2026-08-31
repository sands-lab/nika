from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_api
from nika.service.pingmesh import engine as pingmesh_engine
from nika.utils.errors import safe_tool

mcp = FastMCP("pingmesh_mcp_server")


@safe_tool
@mcp.tool()
async def run_pingmesh_snapshot(
    sources: list[str] | None = None,
    targets: list[str] | None = None,
    count: int = 4,
    high_latency_ms: float = 100.0,
    max_pairs: int = 64,
) -> str:
    """Run an on-demand PingMesh snapshot across endpoint hosts in the current lab.

    Probes reachability, loss, and RTT between endpoint hosts (client/pc/host/server;
    routers and switches excluded by default). Returns a matrix, anomaly pairs, and summary.

    Args:
        sources: Probe sources (default: all discovered endpoints).
        targets: Probe targets (default: all discovered endpoints).
        count: Ping packets per pair (1-20; default 4).
        high_latency_ms: RTT average threshold for high-latency anomalies (default 100).
        max_pairs: Maximum source-target pairs to probe (default 64).
    """
    snapshot = await pingmesh_engine.run_pingmesh_snapshot(
        get_lab_api(),
        sources=sources,
        targets=targets,
        count=count,
        high_latency_ms=high_latency_ms,
        max_pairs=max_pairs,
    )
    return pingmesh_engine.snapshot_to_json(snapshot)


if __name__ == "__main__":
    mcp.run(transport="stdio")
