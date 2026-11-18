"""Typed structures for PingMesh snapshot results."""

from __future__ import annotations

from typing import Literal, TypedDict

PingStatus = Literal["ok", "down", "unstable", "unknown", "timeout", "error"]
AnomalyType = Literal[
    "policy_violation",
    "unreachable",
    "packet_loss",
    "high_latency",
    "unknown",
]


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


class PingPairResult(TypedDict, total=False):
    source: str
    target: str
    loss_percent: float | None
    rtt_avg_ms: float | None
    status: PingStatus
    # Present only for pairs the scenario isolates by design.
    expected: Literal["isolated"]


class PingAnomaly(TypedDict):
    source: str
    target: str
    type: AnomalyType
    detail: str


class PingMeshSummary(TypedDict, total=False):
    candidate_pairs: int
    total_pairs: int
    capped: bool
    reachable_pairs: int
    anomaly_count: int
    policy_violation: int
    unreachable: int
    packet_loss: int
    high_latency: int
    unknown: int
    isolated_pairs: int
    isolated_confirmed: int
    latency_threshold_ms: float
    rtt_ms: dict[str, float]


class PingMeshSnapshot(TypedDict, total=False):
    timestamp: str
    summary: PingMeshSummary
    anomalies: list[PingAnomaly]
    anomalies_omitted: int
    endpoints: dict[str, str | None]
    sources: list[str]
    targets: list[str]
    results: list[PingPairResult]
    note: str
