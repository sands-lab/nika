"""Deep probe helpers for gray and statistical failure symptoms."""

from __future__ import annotations

from typing import Any

from nika.net_env.verify import ping_stats
from nika.problems.support.probe_paths import ProbePath
from nika.runtime.base import LabRuntime


def probe_gray_packet_loss(
    runtime: LabRuntime,
    path: ProbePath,
    *,
    min_loss_percent: float = 0.5,
    max_loss_percent: float = 15.0,
) -> tuple[bool, dict[str, Any]]:
    """Statistical gray-loss probe for silent egress / low-rate drop faults."""
    if not path.dst_ip:
        return False, {"error": "missing_dst_ip"}
    stats = ping_stats(
        runtime,
        path.src_host,
        path.dst_ip,
        count=path.gray_ping_count,
        interval_sec=0.05,
    )
    ok = min_loss_percent <= stats.loss_percent <= max_loss_percent
    return ok, {
        "loss_percent": stats.loss_percent,
        "received": stats.received,
        "transmitted": stats.transmitted,
        "min_loss_percent": min_loss_percent,
        "max_loss_percent": max_loss_percent,
        "raw_tail": stats.raw[-400:],
    }
