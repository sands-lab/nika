from mcp.server.fastmcp import FastMCP

from nika.mcp.session_context import get_lab_api, get_session_meta
from nika.net_env.probe_intent import ProbeIntent
from nika.service.pingmesh import engine as pingmesh_engine

mcp = FastMCP("pingmesh_mcp_server")


@mcp.tool()
async def run_pingmesh_snapshot(
    sources: list[str] | None = None,
    targets: list[str] | None = None,
    count: int = 4,
    high_latency_ms: float = 100.0,
    max_pairs: int = 64,
    include_healthy: bool = False,
) -> str:
    """Run an on-demand PingMesh snapshot across endpoint hosts in the current lab.

    Probes reachability, loss, and RTT between endpoint hosts (client/pc/host/server;
    routers and switches excluded by default). Pairs are sampled evenly across
    sources; the summary reports the candidate pair count and whether `max_pairs`
    capped the probe. Pairs the network design intentionally isolates are probed
    separately and flagged only if they become reachable (policy_violation); the
    latency threshold is never set below the design RTT when the network declares
    one. Returns summary, anomalies, then (optionally) per-pair rows.

    Args:
        sources: Probe sources (default: all discovered endpoints).
        targets: Probe targets (default: all discovered endpoints).
        count: Ping packets per pair (1-20; default 4).
        high_latency_ms: RTT average threshold for high-latency anomalies (default 100).
        max_pairs: Maximum source-target pairs to probe (default 64).
        include_healthy: Also return a compact row for every probed pair
            (default false: only the summary and anomalies).
    """
    snapshot = await pingmesh_engine.run_pingmesh_snapshot(
        get_lab_api(),
        sources=sources,
        targets=targets,
        count=count,
        high_latency_ms=high_latency_ms,
        max_pairs=max_pairs,
        intent=ProbeIntent.from_metadata(get_session_meta().get("metadata")),
        include_healthy=include_healthy,
    )
    return pingmesh_engine.snapshot_to_json(snapshot)


if __name__ == "__main__":
    mcp.run(transport="stdio")
