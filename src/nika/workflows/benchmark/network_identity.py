"""Network grouping and resolved dataset metadata for split selection."""

from __future__ import annotations

import json
from typing import Any

from nika.net_env.isp.identity import is_isp_scenario, isp_topo_from_scenario
from nika.net_env.isp.profiles import default_device_profile
from nika.net_env.net_env_pool import (
    resolve_scenario_backend,
    resolve_scenario_topo_size,
    scenario_family,
    scenario_device_profiles,
    scenario_containerlab_device_profiles,
)
from nika.topology.catalog import isp_topology_size
from nika.workflows.benchmark.candidate_context import failure_domain_for
from nika.workflows.benchmark.multi_fault import row_problems

DATASET_METADATA_FIELDS = (
    "dataset_split",
    "router_platform",
    "family",
    "network_id",
    "topo_scale",
    "failure_domain",
    "slice",
    "base_task_id",
    "vendor_tier",
    "core_task_ids",
    "size_comparison_excluded",
    "combination",
)

MAIN_SLICES = frozenset({"core", "healthy", "multi_fault"})
OPTIONAL_SLICES = frozenset({"vendor_twin", "third_size"})
SIZE_EXCEPTIONS = frozenset(
    {"bgp_address_family_not_activated", "bgp_session_policy_missing"}
)
MATCH_FIELDS = ("igp", "bgp_mode", "rpki", "traffic", "telemetry", "objectives")
VENDORS = ("iosxr", "routeros", "sonic", "nokia_srlinux")
VENDOR_TWIN_LIMIT = 156
TIER_A_GRAPHS = frozenset({"dfn-bwin", "dfn-gwin", "di-yuan", "pdh"})
TIER_A_FAULTS = frozenset(
    {
        "bgp_acl_block",
        "bgp_asn_misconfig",
        "bgp_md5_password_mismatch",
        "bgp_missing_route_advertisement",
        "host_static_blackhole",
        "isis_router_level_mismatch",
        "ospf_acl_block",
        "ospf_area_misconfiguration",
        "ospf_duplicate_router_id",
        "ospf_interface_not_enabled",
    }
)


def is_tier_a_cell(row: dict[str, Any]) -> bool:
    """Identify the release's matched ISP-backbone vendor comparison cells."""
    return row["problem"] in TIER_A_FAULTS and network_identity(row) in {
        ("isp_graph", g) for g in TIER_A_GRAPHS
    }


def is_main_task(row: dict[str, Any]) -> bool:
    return row.get("slice", "core") in MAIN_SLICES


def size_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    """The deployment and observation contract held fixed across sizes."""
    if "backend" not in row or "device_profile" not in row:
        row = dataset_row(row)
    return (
        row["family"],
        row["scenario"] if row["family"] != "isp" else "isp",
        row["backend"],
        row["device_profile"],
        json.dumps({k: row.get(k) for k in MATCH_FIELDS}, sort_keys=True),
    )


def twin_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    """A twin may change only the backend and platform of its core base."""
    return (
        row["network_id"],
        row["scenario"],
        row["problem"],
        json.dumps({k: row.get(k) for k in ("inject", *MATCH_FIELDS)}, sort_keys=True),
    )


def network_identity(row: dict[str, Any]) -> tuple[str, str]:
    """Group all vendors, backends and protocol variants of one graph and size."""
    scenario = str(row["scenario"])
    if is_isp_scenario(scenario):
        return "isp_graph", isp_topo_from_scenario(scenario)
    size = resolve_scenario_topo_size(scenario, row.get("topo_size") or None)
    return scenario, size or "fixed"


def dataset_row(row: dict[str, Any], *, slice_name: str = "core") -> dict[str, Any]:
    """Resolve split metadata without using agent outcomes or authored metadata."""
    scenario = str(row["scenario"])
    identity = network_identity(row)
    backend = resolve_scenario_backend(
        scenario, backend=row.get("backend") or None, default_when_ambiguous="kathara"
    )
    scale = (
        isp_topology_size(identity[1]) if identity[0] == "isp_graph" else identity[1]
    )
    return {
        **row,
        "family": scenario_family(scenario),
        "network_id": ":".join(identity),
        "topo_scale": scale,
        "device_profile": row.get("device_profile") or default_device_profile(backend),
        "router_platform": (
            row.get("device_profile") or default_device_profile(backend)
            if (
                is_isp_scenario(scenario)
                or scenario_device_profiles(scenario)
                or scenario_containerlab_device_profiles(scenario)
            )
            else "none"
        ),
        "backend": backend,
        "problem": "+".join(row_problems(row)),
        "failure_domain": "+".join(
            sorted({failure_domain_for(p) for p in row_problems(row)} - {""})
        ),
        "slice": slice_name,
    }
