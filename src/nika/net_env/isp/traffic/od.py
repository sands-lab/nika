"""Map TrafficMatrixSeries intervals to OD dicts for stub hosts."""

from __future__ import annotations

from typing import Any, Mapping

from nika.net_env.isp.igp.plan import slugify
from nika.net_env.isp.traffic.models import TrafficMatrixSeries, stub_host_name


def series_to_od_dicts(
    series: TrafficMatrixSeries,
    *,
    scale: float = 1.0,
    device_by_node: Mapping[str, str] | None = None,
    inventory: Mapping[str, Any] | None = None,
) -> list[dict[str, dict[str, int]]]:
    """Convert each interval into ``{pc_src: {pc_dst: rate_int}}``.

    Rates are ``max(1, round(raw * scale))`` when positive after scaling.
    """
    mapping = dict(device_by_node or {})
    if not mapping and inventory is not None:
        for row in inventory.get("nodes") or []:
            if row.get("node_id") and row.get("device"):
                mapping[str(row["node_id"])] = str(row["device"])
    if not mapping:
        # Fall back to slugify of node ids present in the series.
        for nid in series.active_node_ids():
            mapping[nid] = slugify(nid, kind="node")

    result: list[dict[str, dict[str, int]]] = []
    for interval in series.intervals:
        od: dict[str, dict[str, int]] = {}
        for flow in interval.flows:
            if flow.src_node_id == flow.dst_node_id:
                continue
            src_dev = mapping.get(flow.src_node_id)
            dst_dev = mapping.get(flow.dst_node_id)
            if not src_dev or not dst_dev:
                continue
            rate = int(round(float(flow.rate) * float(scale)))
            if rate <= 0:
                continue
            src_host = stub_host_name(src_dev)
            dst_host = stub_host_name(dst_dev)
            od.setdefault(src_host, {})[dst_host] = rate
        result.append(od)
    return result
