"""Typed ICMP probe observations."""

from __future__ import annotations

from typing import Literal, TypedDict

PingStatus = Literal["ok", "down", "unstable", "unknown", "timeout", "error"]


class PingStats(TypedDict, total=False):
    tx: int | None
    rx: int | None
    loss_percent: float | None
    time_ms: float | None
    rtt_min_ms: float | None
    rtt_avg_ms: float | None
    rtt_max_ms: float | None
    rtt_mdev_ms: float | None
    status: PingStatus
    detail: str
