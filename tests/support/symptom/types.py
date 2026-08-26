"""Types for test-path failure symptom evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from nika.problems.support.probe_paths import ProbePath

ProbeKind = Literal[
    "path_ping",
    "path_http",
    "path_ping_loss",
    "path_mtu_blackhole",
    "path_mtu_frag_needed",
    "gray_ping_loss",
    "control_plane_bgp",
    "control_plane_ospf",
    "isolation_http",
    "degradation_http",
    "http_by_name",
    "http_body_time",
    "iperf_throughput",
    "route_get_onlink",
    "ping_old_ip",
    "artifact_only",
    "custom",
]

SymptomClass = Literal[
    "unreachable",
    "loss",
    "latency",
    "gray",
    "control_plane",
    "isolation",
    "degradation",
    "none",
]


@dataclass
class ProbeSnapshot:
    """Serializable probe measurements."""

    ping_ok: bool | None = None
    http_ok: bool | None = None
    loss_percent: float | None = None
    rtt_avg_ms: float | None = None
    rtt_mdev_ms: float | None = None
    http_time_ms: float | None = None
    mtu_blackhole: bool | None = None
    mtu_frag_needed: bool | None = None
    control_plane_ok: bool | None = None
    symptom_ok: bool | None = None
    control_ok: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ping_ok": self.ping_ok,
            "http_ok": self.http_ok,
            "loss_percent": self.loss_percent,
            "rtt_avg_ms": self.rtt_avg_ms,
            "rtt_mdev_ms": self.rtt_mdev_ms,
            "http_time_ms": self.http_time_ms,
            "mtu_blackhole": self.mtu_blackhole,
            "mtu_frag_needed": self.mtu_frag_needed,
            "control_plane_ok": self.control_plane_ok,
            "symptom_ok": self.symptom_ok,
            "control_ok": self.control_ok,
            **self.extra,
        }


__all__ = [
    "ProbeKind",
    "ProbePath",
    "ProbeSnapshot",
    "SymptomClass",
]
