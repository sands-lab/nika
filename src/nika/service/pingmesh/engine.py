"""Run on-demand PingMesh snapshots across lab endpoints."""

from __future__ import annotations

import asyncio
import json
import statistics
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from nika.net_env.probe_intent import ProbeIntent
from nika.service.pingmesh.endpoints import discover_endpoints, resolve_endpoint_ip
from nika.service.pingmesh.parser import parse_ping_output
from nika.service.pingmesh.types import (
    AnomalyType,
    PingAnomaly,
    PingMeshSnapshot,
    PingMeshSummary,
    PingPairResult,
    PingStats,
)
from nika.service.shell import ping_exec_timeout

MIN_COUNT = 1
MAX_COUNT = 20
DEFAULT_COUNT = 4
DEFAULT_HIGH_LATENCY_MS = 100.0
DEFAULT_MAX_PAIRS = 64
# Share of the pair budget reserved for pairs the scenario isolates by design,
# so policy violations stay observable without crowding out expected pairs.
ISOLATED_PAIR_SHARE = 4
# Bounds snapshot size so the agent-facing tool output is never truncated.
MAX_LISTED_ANOMALIES = 40
RESOLVE_TIMEOUT_SEC = 20.0
# Concurrent execs stay below the default thread-pool size so queued probes are
# not mistaken for timed-out ones.
MAX_CONCURRENT_PROBES = 16
# Seconds past the last echo request before ping gives up on a silent pair.
PING_DEADLINE_SLACK_SEC = 2
# Worst-case seconds for the probe waves. With RESOLVE_TIMEOUT_SEC it stays
# under the default MCP tool timeout (nika.mcp.read_timeout_sec, 120 s), so a
# snapshot returns results instead of timing out the agent's tool call.
PROBE_BUDGET_SEC = 90.0
EXEC_TIMEOUT_SENTINEL = "[TIMEOUT]"
_ANOMALY_ORDER: dict[AnomalyType, int] = {
    "policy_violation": 0,
    "unreachable": 1,
    "unknown": 2,
    "packet_loss": 3,
    "high_latency": 4,
}


def _clamp_count(count: int) -> int:
    return max(MIN_COUNT, min(MAX_COUNT, count))


def _pair_ceiling(count: int) -> int:
    """Most pairs whose probe waves fit in PROBE_BUDGET_SEC at worst case."""
    wave_sec = ping_exec_timeout(count) + 5
    return MAX_CONCURRENT_PROBES * max(1, int(PROBE_BUDGET_SEC // wave_sec))


def _validate_selection(
    selected: list[str] | None,
    discovered: list[str],
    label: str,
) -> list[str]:
    if selected is None:
        return discovered
    unknown = sorted(set(selected) - set(discovered))
    if unknown:
        raise ValueError(
            f"Unknown {label}: {unknown}. Available endpoints: {discovered}"
        )
    return selected


def _round_robin_pairs(
    sources: list[str],
    targets: list[str],
    include: Callable[[str, str], bool],
) -> list[tuple[str, str]]:
    """Pairs ordered so every prefix covers all sources and spreads over targets.

    Each source walks the target list starting after its own position, and the
    walks are interleaved one pair per source per round. Cutting the result at
    any length therefore samples every source evenly instead of exhausting the
    first sources alphabetically.
    """
    walks: list[list[tuple[str, str]]] = []
    for index, src in enumerate(sources):
        start = targets.index(src) + 1 if src in targets else index
        rotated = targets[start:] + targets[:start]
        walks.append(
            [(src, dst) for dst in rotated if dst != src and include(src, dst)]
        )
    rounds = max((len(walk) for walk in walks), default=0)
    return [walk[r] for r in range(rounds) for walk in walks if r < len(walk)]


def _build_pairs(
    sources: list[str],
    targets: list[str],
    max_pairs: int,
    *,
    is_isolated: Callable[[str, str], bool] | None = None,
) -> tuple[list[tuple[str, str]], int]:
    """Return ``(pairs to probe, candidate pair count)``.

    Pairs the scenario isolates by design are sampled separately and capped at a
    quarter of the budget (unused budget flows back to expected pairs).
    """
    isolated = is_isolated or (lambda _src, _dst: False)
    expected = _round_robin_pairs(sources, targets, lambda a, b: not isolated(a, b))
    blocked = _round_robin_pairs(sources, targets, isolated)
    candidates = len(expected) + len(blocked)
    max_pairs = max(max_pairs, 0)
    blocked_quota = min(len(blocked), max_pairs // ISOLATED_PAIR_SHARE)
    expected_quota = min(len(expected), max_pairs - blocked_quota)
    blocked_quota = min(len(blocked), max_pairs - expected_quota)
    return expected[:expected_quota] + blocked[:blocked_quota], candidates


def _row_reachable(row: PingPairResult) -> bool:
    return row.get("status") in ("ok", "unstable")


def _failed_stats(status: str, detail: str) -> PingStats:
    return {"status": status, "detail": detail}  # type: ignore[typeddict-item]


def _anomaly(source: str, target: str, kind: AnomalyType, detail: str) -> PingAnomaly:
    return {"source": source, "target": target, "type": kind, "detail": detail}


def _classify_anomalies(
    source: str,
    target: str,
    stats: PingStats,
    *,
    high_latency_ms: float,
    isolated: bool = False,
) -> list[PingAnomaly]:
    status = stats.get("status", "unknown")
    loss = stats.get("loss_percent")
    rx = stats.get("rx")
    rtt_avg = stats.get("rtt_avg_ms")

    if status in ("unknown", "timeout", "error"):
        detail = stats.get("detail") or "Unable to parse ping output"
        return [_anomaly(source, target, "unknown", detail)]

    unreachable = status == "down" or loss == 100 or rx == 0
    if isolated:
        if unreachable:
            return []
        return [
            _anomaly(
                source,
                target,
                "policy_violation",
                "reachable although the scenario design isolates this pair",
            )
        ]

    if unreachable:
        detail = f"{loss or 100}% packet loss" if loss is not None else "no replies"
        return [_anomaly(source, target, "unreachable", detail)]

    anomalies: list[PingAnomaly] = []
    if (loss is not None and 0 < loss < 100) or status == "unstable":
        anomalies.append(
            _anomaly(source, target, "packet_loss", f"{loss}% packet loss")
        )
    if rtt_avg is not None and rtt_avg > high_latency_ms:
        anomalies.append(
            _anomaly(
                source,
                target,
                "high_latency",
                f"avg RTT {rtt_avg}ms exceeds {high_latency_ms:g}ms",
            )
        )
    return anomalies


def _rtt_summary(results: list[PingPairResult]) -> dict[str, float]:
    rtts = [
        row["rtt_avg_ms"]
        for row in results
        if _row_reachable(row)
        and row.get("expected") != "isolated"
        and row.get("rtt_avg_ms") is not None
    ]
    if not rtts:
        return {}
    return {
        "min": round(min(rtts), 3),
        "median": round(statistics.median(rtts), 3),
        "max": round(max(rtts), 3),
    }


def _build_summary(
    results: list[PingPairResult],
    anomalies: list[PingAnomaly],
    *,
    candidate_pairs: int,
    latency_threshold_ms: float,
) -> PingMeshSummary:
    counts: dict[AnomalyType, int] = dict.fromkeys(_ANOMALY_ORDER, 0)
    for anomaly in anomalies:
        counts[anomaly["type"]] += 1

    isolated_rows = [row for row in results if row.get("expected") == "isolated"]
    summary: PingMeshSummary = {
        "candidate_pairs": candidate_pairs,
        "total_pairs": len(results),
        "capped": len(results) < candidate_pairs,
        "reachable_pairs": sum(
            1
            for row in results
            if _row_reachable(row) and row.get("expected") != "isolated"
        ),
        "anomaly_count": len(anomalies),
        "policy_violation": counts["policy_violation"],
        "unreachable": counts["unreachable"],
        "packet_loss": counts["packet_loss"],
        "high_latency": counts["high_latency"],
        "unknown": counts["unknown"],
        "latency_threshold_ms": latency_threshold_ms,
    }
    if isolated_rows:
        summary["isolated_pairs"] = len(isolated_rows)
        summary["isolated_confirmed"] = sum(
            1 for row in isolated_rows if row.get("status") == "down"
        )
    rtt = _rtt_summary(results)
    if rtt:
        summary["rtt_ms"] = rtt
    return summary


def _stats_to_pair_result(
    source: str,
    target: str,
    stats: PingStats,
    *,
    isolated: bool = False,
) -> PingPairResult:
    row: PingPairResult = {
        "source": source,
        "target": target,
        "loss_percent": stats.get("loss_percent"),
        "rtt_avg_ms": stats.get("rtt_avg_ms"),
        "status": stats.get("status", "unknown"),
    }
    if isolated:
        row["expected"] = "isolated"
    return row


async def _probe_pair(
    api: Any,
    source: str,
    target_ip: str | None,
    count: int,
    slots: asyncio.Semaphore,
) -> PingStats:
    """Probe one pair; a failure of this pair never aborts the rest."""
    if not target_ip:
        return _failed_stats("unknown", "target address could not be resolved")
    budget = ping_exec_timeout(count)
    deadline = count + PING_DEADLINE_SLACK_SEC
    command = f"ping -c {count} -w {deadline} -n -q {target_ip}"
    try:
        async with slots:
            output = await asyncio.wait_for(
                api.exec_cmd_async(source, command, timeout=budget),
                timeout=budget + 5,
            )
    except asyncio.TimeoutError:
        return _failed_stats("timeout", f"probe from {source} timed out")
    except Exception as exc:  # noqa: BLE001 - one pair must not fail the snapshot
        return _failed_stats(
            "error", f"probe from {source} failed: {type(exc).__name__}"
        )
    if not isinstance(output, str):
        return _failed_stats("unknown", "Unable to parse ping output")
    if output.startswith(EXEC_TIMEOUT_SENTINEL):
        return _failed_stats("timeout", f"probe from {source} timed out")
    stats = parse_ping_output(output)
    if stats.get("status") == "unknown":
        excerpt = " ".join(output.split())[:100]
        stats["detail"] = (
            f"unparsable ping output: {excerpt}" if excerpt else "no ping output"
        )
    return stats


async def _resolve_endpoint_ips(api: Any, names: list[str]) -> dict[str, str | None]:
    """Resolve probe addresses concurrently; an unresolvable endpoint maps to None."""

    async def resolve(name: str) -> str | None:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(resolve_endpoint_ip, api, name),
                timeout=RESOLVE_TIMEOUT_SEC,
            )
        except Exception:  # noqa: BLE001 - includes timeouts and exec failures
            return None

    addresses = await asyncio.gather(*(resolve(name) for name in names))
    return dict(zip(names, addresses))


async def run_pingmesh_snapshot(
    api: Any,
    *,
    sources: list[str] | None = None,
    targets: list[str] | None = None,
    count: int = DEFAULT_COUNT,
    high_latency_ms: float = DEFAULT_HIGH_LATENCY_MS,
    max_pairs: int = DEFAULT_MAX_PAIRS,
    intent: ProbeIntent | None = None,
    include_healthy: bool = False,
) -> PingMeshSnapshot:
    """Execute a PingMesh snapshot and return structured results.

    ``intent`` carries the scenario's designed isolation and RTT; pairs it
    isolates are reported as ``policy_violation`` only if they become reachable,
    and the latency threshold never drops below the design RTT plus headroom.
    Per-pair rows are returned only with ``include_healthy``.
    """
    discovered = discover_endpoints(api)
    if not discovered:
        raise ValueError("No endpoint hosts found in the current lab session.")

    resolved_sources = _validate_selection(sources, discovered, "sources")
    resolved_targets = _validate_selection(targets, discovered, "targets")
    if not resolved_sources:
        raise ValueError("No source endpoints selected for PingMesh.")
    if not resolved_targets:
        raise ValueError("No target endpoints selected for PingMesh.")

    count = _clamp_count(count)
    threshold_ms = (
        intent.latency_threshold_ms(high_latency_ms) if intent else high_latency_ms
    )
    is_isolated = intent.isolation_matcher() if intent else (lambda _a, _b: False)
    pairs, candidate_pairs = _build_pairs(
        resolved_sources,
        resolved_targets,
        min(max_pairs, _pair_ceiling(count)),
        is_isolated=is_isolated,
    )
    if not pairs:
        raise ValueError(
            "PingMesh needs at least two distinct endpoint hosts; no pairs can be probed."
        )

    probed = sorted({name for pair in pairs for name in pair})
    endpoint_ips = await _resolve_endpoint_ips(api, probed)
    slots = asyncio.Semaphore(MAX_CONCURRENT_PROBES)
    stats_list = await asyncio.gather(
        *(
            _probe_pair(api, src, endpoint_ips.get(dst), count, slots)
            for src, dst in pairs
        )
    )

    results: list[PingPairResult] = []
    anomalies: list[PingAnomaly] = []
    for (src, dst), stats in zip(pairs, stats_list):
        isolated = is_isolated(src, dst)
        results.append(_stats_to_pair_result(src, dst, stats, isolated=isolated))
        anomalies.extend(
            _classify_anomalies(
                src,
                dst,
                stats,
                high_latency_ms=threshold_ms,
                isolated=isolated,
            )
        )

    summary = _build_summary(
        results,
        anomalies,
        candidate_pairs=candidate_pairs,
        latency_threshold_ms=threshold_ms,
    )
    anomalies.sort(key=lambda row: _ANOMALY_ORDER[row["type"]])
    snapshot: PingMeshSnapshot = {
        "timestamp": datetime.now(UTC).isoformat(),
        "summary": summary,
        "anomalies": anomalies[:MAX_LISTED_ANOMALIES],
    }
    if len(anomalies) > MAX_LISTED_ANOMALIES:
        snapshot["anomalies_omitted"] = len(anomalies) - MAX_LISTED_ANOMALIES
    snapshot["endpoints"] = {name: endpoint_ips[name] for name in probed}
    snapshot["sources"] = resolved_sources
    snapshot["targets"] = resolved_targets
    if include_healthy:
        snapshot["results"] = results
    else:
        snapshot["note"] = (
            "Healthy per-pair rows omitted; pass include_healthy=true for every probed pair."
        )
    return snapshot


def snapshot_to_json(snapshot: PingMeshSnapshot) -> str:
    return json.dumps(snapshot, separators=(",", ":"))
