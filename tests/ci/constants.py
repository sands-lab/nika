"""Shared CI smoke scenario and image lists (equal coverage on amd64 and arm64).

Curated cases use published net-env pool scenarios and the same inject/verify
workflows a user would run locally (``nika env run`` / failure inject /
``evaluate_scenario``). Test-only fixtures such as ``simple_bgp`` are excluded.
"""

from __future__ import annotations

from nika.net_env.utils.kathara.docker_files.docker_images import (
    NIKA_IMAGE_DOCKERFILES,
)

# Locally buildable nika/* images exercised by the image matrix.
CI_NIKA_IMAGES: tuple[str, ...] = tuple(NIKA_IMAGE_DOCKERFILES.keys())

# Kathara light-startup scenarios (topo_size=s). Same list on both arches.
CI_KATHARA_STARTUP_SCENARIOS: tuple[str, ...] = (
    "dc_clos",
    "campus_lan",
    "enterprise_branch",
    "sdn_l3_clos",
    "p4_dc_fabric",
    "p4_dc_gateway",
)

# Full behavioral verify (evaluate_scenario) subset for PR CI.
CI_KATHARA_VERIFY_SCENARIOS: tuple[str, ...] = (
    "dc_clos",
    "campus_lan",
    "sdn_l3_clos",
)

# Curated failure-inject smoke: (case_id, scenario, problem, topo_size, backend).
# Inject params resolve via benchmark/problem defaults (same as local contract tests).
CI_FAILURE_INJECT_CASES: tuple[tuple[str, str, str, str | None, str], ...] = (
    ("dc_clos-link_down", "dc_clos", "link_down", "s", "kathara"),
    ("dc_clos-link_flap", "dc_clos", "link_flap", "s", "kathara"),
    ("campus_lan-ospf_neighbor_missing", "campus_lan", "ospf_neighbor_missing", "s", "kathara"),
    ("campus_lan-dhcp_service_down", "campus_lan", "dhcp_service_down", "s", "kathara"),
    (
        "enterprise_branch-link_packet_corruption",
        "enterprise_branch",
        "link_packet_corruption",
        "s",
        "kathara",
    ),
    (
        "p4_dc_gateway-link_capacity_bottleneck",
        "p4_dc_gateway",
        "link_capacity_bottleneck",
        "s",
        "kathara",
    ),
    ("sdn_l3_clos-link_down", "sdn_l3_clos", "link_down", "s", "kathara"),
    ("min3clos-link_down", "min3clos", "link_down", None, "containerlab"),
    ("min3clos-bgp_asn_misconfig", "min3clos", "bgp_asn_misconfig", None, "containerlab"),
)

CI_KATHARA_FAILURE_CASE_IDS: tuple[str, ...] = tuple(
    case_id
    for case_id, _scenario, _problem, _topo, backend in CI_FAILURE_INJECT_CASES
    if backend == "kathara"
)
CI_CLAB_FAILURE_CASE_IDS: tuple[str, ...] = tuple(
    case_id
    for case_id, _scenario, _problem, _topo, backend in CI_FAILURE_INJECT_CASES
    if backend == "containerlab"
)
