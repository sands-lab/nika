"""ISP SNDlib traffic: unified series, cache, stubs, OD mapping."""

from nika.net_env.isp.traffic.cache import (
    DYNAMIC_TRAFFIC_CATALOG,
    default_cache_root,
    dynamic_cache_dir,
    fetch_dynamic_traffic,
    load_dynamic_series,
    write_normalized_series,
)
from nika.net_env.isp.traffic.models import (
    DEFAULT_DEMAND_INTERVAL_SEC,
    DEFAULT_EDGE_POOL_CIDR,
    DEFAULT_TRAFFIC_MODE,
    DEFAULT_TRAFFIC_SCALE,
    DEFAULT_TRAFFIC_UNIT,
    SUPPORTED_TRAFFIC_MODES,
    TrafficFlow,
    TrafficInterval,
    TrafficMatrixSeries,
    TrafficMode,
    normalize_traffic_mode,
    stub_host_name,
)
from nika.net_env.isp.traffic.od import series_to_od_dicts
from nika.net_env.isp.traffic.resolve import resolve_traffic_series, series_from_demands
from nika.net_env.isp.traffic.stubs import (
    IspTrafficAttachment,
    PlannedEdgeLink,
    PlannedHost,
    attach_traffic_stubs,
    remap_inventory_ifaces_to_srl,
)

__all__ = [
    "DEFAULT_DEMAND_INTERVAL_SEC",
    "DEFAULT_EDGE_POOL_CIDR",
    "DEFAULT_TRAFFIC_MODE",
    "DEFAULT_TRAFFIC_SCALE",
    "DEFAULT_TRAFFIC_UNIT",
    "DYNAMIC_TRAFFIC_CATALOG",
    "IspTrafficAttachment",
    "PlannedEdgeLink",
    "PlannedHost",
    "SUPPORTED_TRAFFIC_MODES",
    "TrafficFlow",
    "TrafficInterval",
    "TrafficMatrixSeries",
    "TrafficMode",
    "attach_traffic_stubs",
    "default_cache_root",
    "dynamic_cache_dir",
    "fetch_dynamic_traffic",
    "load_dynamic_series",
    "normalize_traffic_mode",
    "remap_inventory_ifaces_to_srl",
    "resolve_traffic_series",
    "series_from_demands",
    "series_to_od_dicts",
    "stub_host_name",
    "write_normalized_series",
]
