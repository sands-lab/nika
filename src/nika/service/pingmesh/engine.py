"""Run on-demand PingMesh snapshots across lab endpoints."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from nika.service.pingmesh.endpoints import discover_endpoints, resolve_endpoint_ip
from nika.service.pingmesh.parser import parse_ping_output
from nika.service.shell import ping_exec_timeout
from nika.service.pingmesh.types import (
    AnomalyType,
    PingAnomaly,
    PingMeshSnapshot,
    PingMeshSummary,
    PingPairResult,
    PingStats,
)

MIN_COUNT = 1
MAX_COUNT = 20
DEFAULT_COUNT = 4
DEFAULT_HIGH_LATENCY_MS = 100.0
DEFAULT_MAX_PAIRS = 64


def _clamp_count(count: int) -> int:
    return max(MIN_COUNT, min(MAX_COUNT, count))


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


def _build_pairs(
    sources: list[str],
    targets: list[str],
    max_pairs: int,
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for src in sources:
        for dst in targets:
            if src == dst:
                continue
            pairs.append((src, dst))
            if len(pairs) >= max_pairs:
                return pairs
    return pairs


def _is_reachable(stats: PingStats) -> bool:
    status = stats.get("status")
    loss = stats.get("loss_percent")
    rx = stats.get("rx")
    if status == "down" or loss == 100 or rx == 0:
        return False
    if status == "unknown":
        return False
    return True


def _classify_anomalies(
    source: str,
    target: str,
    stats: PingStats,
    *,
    high_latency_ms: float,
) -> list[PingAnomaly]:
    status = stats.get("status", "unknown")
    loss = stats.get("loss_percent")
    rx = stats.get("rx")
    rtt_avg = stats.get("rtt_avg_ms")
    anomalies: list[PingAnomaly] = []

    if status == "unknown":
        anomalies.append(
            {
                "source": source,
                "target": target,
                "type": "unknown",
                "detail": "Unable to parse ping output or resolve target",
            }
        )
        return anomalies

    if status == "down" or loss == 100 or rx == 0:
        detail = f"{loss or 100}% packet loss" if loss is not None else "no replies"
        anomalies.append(
            {
                "source": source,
                "target": target,
                "type": "unreachable",
                "detail": detail,
            }
        )
        return anomalies

    if (loss is not None and 0 < loss < 100) or status == "unstable":
        anomalies.append(
            {
                "source": source,
                "target": target,
                "type": "packet_loss",
                "detail": f"{loss}% packet loss",
            }
        )

    if rtt_avg is not None and rtt_avg > high_latency_ms:
        anomalies.append(
            {
                "source": source,
                "target": target,
                "type": "high_latency",
                "detail": f"avg RTT {rtt_avg}ms exceeds {high_latency_ms}ms",
            }
        )

    return anomalies


def _build_summary(
    results: list[PingPairResult],
    anomalies: list[PingAnomaly],
) -> PingMeshSummary:
    counts: dict[AnomalyType, int] = {
        "unreachable": 0,
        "packet_loss": 0,
        "high_latency": 0,
        "unknown": 0,
    }
    for anomaly in anomalies:
        counts[anomaly["type"]] += 1

    reachable_pairs = sum(1 for row in results if row.get("reachable"))
    return {
        "total_pairs": len(results),
        "reachable_pairs": reachable_pairs,
        "anomaly_count": len(anomalies),
        "unreachable": counts["unreachable"],
        "packet_loss": counts["packet_loss"],
        "high_latency": counts["high_latency"],
        "unknown": counts["unknown"],
    }


def _stats_to_pair_result(
    source: str,
    target: str,
    target_ip: str | None,
    stats: PingStats,
) -> PingPairResult:
    return {
        "source": source,
        "target": target,
        "target_ip": target_ip,
        "reachable": _is_reachable(stats),
        "loss_percent": stats.get("loss_percent"),
        "rtt_min_ms": stats.get("rtt_min_ms"),
        "rtt_avg_ms": stats.get("rtt_avg_ms"),
        "rtt_max_ms": stats.get("rtt_max_ms"),
        "rtt_mdev_ms": stats.get("rtt_mdev_ms"),
        "status": stats.get("status", "unknown"),
    }


async def _probe_pair(
    api: Any,
    source: str,
    target_ip: str,
    count: int,
) -> PingStats:
    command = f"ping -c {count} -n -q {target_ip}"
    output = await api.exec_cmd_async(source, command, timeout=ping_exec_timeout(count))
    return parse_ping_output(output)


async def run_pingmesh_snapshot(
    api: Any,
    *,
    sources: list[str] | None = None,
    targets: list[str] | None = None,
    count: int = DEFAULT_COUNT,
    high_latency_ms: float = DEFAULT_HIGH_LATENCY_MS,
    max_pairs: int = DEFAULT_MAX_PAIRS,
) -> PingMeshSnapshot:
    """Execute a PingMesh snapshot and return structured results."""
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
    endpoint_ips = {name: resolve_endpoint_ip(api, name) for name in discovered}
    pairs = _build_pairs(resolved_sources, resolved_targets, max_pairs)

    coroutines = []
    pair_meta: list[tuple[str, str, str | None]] = []
    for src, dst in pairs:
        dst_ip = endpoint_ips.get(dst)
        pair_meta.append((src, dst, dst_ip))
        if dst_ip:
            coroutines.append(_probe_pair(api, src, dst_ip, count))
        else:
            coroutines.append(asyncio.sleep(0, result=parse_ping_output("")))

    stats_list = await asyncio.gather(*coroutines)

    results: list[PingPairResult] = []
    anomalies: list[PingAnomaly] = []
    for (src, dst, dst_ip), stats in zip(pair_meta, stats_list):
        pair_result = _stats_to_pair_result(src, dst, dst_ip, stats)
        results.append(pair_result)
        anomalies.extend(
            _classify_anomalies(
                src,
                dst,
                stats,
                high_latency_ms=high_latency_ms,
            )
        )

    snapshot: PingMeshSnapshot = {
        "timestamp": datetime.now(UTC).isoformat(),
        "endpoints": endpoint_ips,
        "sources": resolved_sources,
        "targets": resolved_targets,
        "results": results,
        "anomalies": anomalies,
        "summary": _build_summary(results, anomalies),
    }
    return snapshot


def snapshot_to_json(snapshot: PingMeshSnapshot) -> str:
    return json.dumps(snapshot, separators=(",", ":"))
