"""Shared CI smoke scenario and image lists (equal coverage on amd64 and arm64)."""

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

# Curated failure-inject smoke (verify-only / ground-truth).
CI_FAILURE_INJECT_CASES: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("simple_bgp", "link_down", {"host_name": "pc1", "intf_name": "eth0"}),
    (
        "dc_clos",
        "link_down",
        {"host_name": "client_0", "intf_name": "eth0"},
    ),
)
