"""E2E validation helpers for selected / catalog benchmark cases."""

from __future__ import annotations

from nika.net_env.isp.identity import (
    is_isp_base_topology,
    isp_topo_from_scenario,
)

# Containerlab ISP catalog/E2E: keep labs small enough for typical 16Gi hosts
# (SRL routers + PC endpoints; 12-router graphs previously OOM'd).
CONTAINERLAB_ISP_MAX_NODES = 11
_SNDLIB_NODE_COUNTS: dict[str, int] = {
    "dfn-bwin": 10,
    "dfn-gwin": 11,
    "di-yuan": 11,
    "pdh": 11,
    "abilene": 12,
    "polska": 12,
}


def isp_containerlab_node_count(scenario: str) -> int | None:
    """Return SNDlib router count for an ISP base scenario, else None."""
    if not is_isp_base_topology(scenario):
        return None
    topo = isp_topo_from_scenario(scenario)
    return _SNDLIB_NODE_COUNTS.get(topo)


def containerlab_isp_supported(scenario: str) -> bool:
    """True when an ISP base topology is small enough for Containerlab healthy checks."""
    if not is_isp_base_topology(scenario):
        return False
    topo = isp_topo_from_scenario(scenario)
    return _SNDLIB_NODE_COUNTS.get(topo, 999) <= CONTAINERLAB_ISP_MAX_NODES
