"""SNDlib traffic matrix IR for ISP scenarios."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

TrafficMode = Literal["none", "demands", "dynamic"]
TrafficSource = Literal["demands", "dynamic"]

SUPPORTED_TRAFFIC_MODES: tuple[TrafficMode, ...] = ("none", "demands", "dynamic")
DEFAULT_TRAFFIC_MODE: TrafficMode = "none"
DEFAULT_TRAFFIC_SCALE = 1.0
DEFAULT_DEMAND_INTERVAL_SEC = 5
DEFAULT_TRAFFIC_UNIT: Literal["K", "M"] = "K"

# Stub host ↔ router edge LANs (distinct from loopback 10.255/16 and P2P 10.0/8).
DEFAULT_EDGE_POOL_CIDR = "10.254.0.0/16"


@dataclass(frozen=True)
class TrafficFlow:
    """One OD flow in SNDlib node-id space (pre-scale)."""

    src_node_id: str
    dst_node_id: str
    rate: float


@dataclass(frozen=True)
class TrafficInterval:
    index: int
    duration_sec: int
    flows: tuple[TrafficFlow, ...]


@dataclass(frozen=True)
class TrafficMatrixSeries:
    topology: str
    source: TrafficSource
    intervals: tuple[TrafficInterval, ...]
    sample_period_sec: int | None = None
    unit_note: str = "SNDlib planning / trace units (not Mbps)"
    path: str | None = None

    def active_node_ids(self) -> tuple[str, ...]:
        """PoPs that appear as src or dst in any interval (stable sorted)."""
        nodes: set[str] = set()
        for interval in self.intervals:
            for flow in interval.flows:
                if flow.src_node_id != flow.dst_node_id:
                    nodes.add(flow.src_node_id)
                    nodes.add(flow.dst_node_id)
        return tuple(sorted(nodes))

    def summary(self) -> dict[str, Any]:
        return {
            "topology": self.topology,
            "source": self.source,
            "interval_count": len(self.intervals),
            "sample_period_sec": self.sample_period_sec,
            "unit_note": self.unit_note,
            "path": self.path,
            "active_node_count": len(self.active_node_ids()),
            "flow_counts": [len(i.flows) for i in self.intervals],
        }


def normalize_traffic_mode(raw: str | TrafficMode | None) -> TrafficMode:
    if raw is None or raw == "":
        return DEFAULT_TRAFFIC_MODE
    mode = str(raw).strip().lower()
    if mode not in SUPPORTED_TRAFFIC_MODES:
        raise ValueError(
            f"Unsupported traffic_mode {raw!r}; expected one of {SUPPORTED_TRAFFIC_MODES}."
        )
    return mode  # type: ignore[return-value]


def stub_host_name(router_device: str) -> str:
    """Kathara host name for the stub attached to ``router_device``."""
    return f"pc_{router_device}"
