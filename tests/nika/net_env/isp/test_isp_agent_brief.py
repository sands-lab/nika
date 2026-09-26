"""Agent-facing ISP network brief granularity across all ISP scenarios."""

from __future__ import annotations

import pytest

from nika.net_env.net_env_pool import (
    _NET_ENV_SPECS,
    get_net_env_instance,
    list_all_net_envs,
)

_REQUIRED = (
    "Network Description:",
    "PCs:",
    "Routers (",
    "Topology:",
    "SNDlib topology:",
    "IGP:",
    "constant_metric:",
    "BGP mode:",
    "device_profile:",
    "Inventory nodes:",
    "traffic matrix chosen at `nika traffic run sndlib`",
)


def _isp_scenario_backends() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for name in sorted(n for n in list_all_net_envs() if n.startswith("isp_")):
        for backend in _NET_ENV_SPECS[name].supported_backends:
            pairs.append((name, backend))
    return pairs


@pytest.mark.parametrize(("scenario", "backend"), _isp_scenario_backends())
def test_all_isp_briefs_share_inventory_topology_contract(
    scenario: str, backend: str
) -> None:
    env = get_net_env_instance(scenario, backend=backend)
    info = env.get_info()
    missing = [key for key in _REQUIRED if key not in info]
    assert not missing, f"{scenario}/{backend} missing {missing}"
    assert env.inventory["link_count"] + len(env.inventory.get("hosts") or []) > 0
    # Topology line must list endpoint pairs, not counts only.
    topo_section = info.split("Topology:", 1)[1]
    assert "(" in topo_section and ")" in topo_section
    if backend == "containerlab":
        assert "Routers (Nokia SR Linux):" in info
        assert "device_profile: nokia_srlinux" in info
    else:
        assert "Routers (FRRRouting):" in info
        assert "device_profile: frr" in info


def test_min3clos_brief_still_lists_nodes() -> None:
    env = get_net_env_instance("min3clos", backend="containerlab")
    info = env.get_info()
    assert "leaf1" in info
    assert "Topology:" in info
