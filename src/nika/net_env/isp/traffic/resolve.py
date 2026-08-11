"""Resolve SNDlib traffic into a unified TrafficMatrixSeries."""

from __future__ import annotations

import logging
from pathlib import Path

from nika.config import REPO_ROOT
from nika.net_env.isp.traffic.cache import (
    dynamic_cache_dir,
    load_dynamic_series,
)
from nika.net_env.isp.traffic.models import (
    DEFAULT_DEMAND_INTERVAL_SEC,
    TrafficFlow,
    TrafficInterval,
    TrafficMatrixSeries,
    TrafficMode,
    normalize_traffic_mode,
)
from nika.topology import NetworkTopology, load_sndlib_topology

logger = logging.getLogger(__name__)


def resolve_traffic_series(
    topology: str | Path | NetworkTopology,
    mode: TrafficMode | str | None = "demands",
    *,
    demand_interval_sec: int = DEFAULT_DEMAND_INTERVAL_SEC,
    cache_root: Path | None = None,
) -> TrafficMatrixSeries | None:
    """Resolve traffic for an ISP topology.

    - ``none`` → ``None``
    - ``demands`` → one interval from XML ``<demands>``
    - ``dynamic`` → cache under ``.nika_cache/sndlib/traffic/<topo>/`` if present;
      otherwise fall back to demands with a warning
    """
    traffic_mode = normalize_traffic_mode(mode)
    if traffic_mode == "none":
        return None

    if isinstance(topology, NetworkTopology):
        topo = topology
        topo_name = topo.name
    else:
        topo = load_sndlib_topology(topology)
        topo_name = topo.name

    if traffic_mode == "dynamic":
        root = cache_root if cache_root is not None else REPO_ROOT / ".nika_cache"
        cache_dir = dynamic_cache_dir(topo_name, cache_root=root)
        try:
            return load_dynamic_series(cache_dir, topology=topo_name)
        except (FileNotFoundError, ValueError, OSError) as exc:
            logger.warning(
                "Dynamic traffic cache unavailable for %s (%s); falling back to demands.",
                topo_name,
                exc,
            )

    return series_from_demands(topo, duration_sec=demand_interval_sec)


def series_from_demands(
    topology: NetworkTopology,
    *,
    duration_sec: int = DEFAULT_DEMAND_INTERVAL_SEC,
) -> TrafficMatrixSeries:
    """Build a single-interval series from SNDlib XML demands."""
    flows: list[TrafficFlow] = []
    for demand in topology.demands:
        if demand.source == demand.target:
            continue
        if demand.demand_value <= 0:
            continue
        flows.append(
            TrafficFlow(
                src_node_id=demand.source,
                dst_node_id=demand.target,
                rate=float(demand.demand_value),
            )
        )
    flows_t = tuple(sorted(flows, key=lambda f: (f.src_node_id, f.dst_node_id, f.rate)))
    interval = TrafficInterval(index=0, duration_sec=duration_sec, flows=flows_t)
    return TrafficMatrixSeries(
        topology=topology.name,
        source="demands",
        intervals=(interval,),
        sample_period_sec=duration_sec,
        unit_note="SNDlib demandValue planning units (not Mbps)",
        path=None,
    )
