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

# Full behavioral verify (evaluate_scenario) — Nightly / local, not PR CI.
CI_KATHARA_VERIFY_SCENARIOS: tuple[str, ...] = (
    "dc_clos",
    "campus_lan",
    "sdn_l3_clos",
)

# PR-curated failure-inject smoke: one Kathara + one Containerlab path.
# Wider inject coverage lives in tests/nika/problems/test_failure_inject_contract.py (Nightly).
CI_FAILURE_INJECT_CASES: tuple[tuple[str, str, str, str | None, str], ...] = (
    ("dc_clos-link_down", "dc_clos", "link_down", "s", "kathara"),
    ("min3clos-link_down", "min3clos", "link_down", None, "containerlab"),
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
