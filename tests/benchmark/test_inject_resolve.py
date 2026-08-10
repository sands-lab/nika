"""Offline inject-target resolution for benchmark YAML generation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BENCHMARK_DIR = Path(__file__).resolve().parents[2] / "benchmark"
if str(_BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCHMARK_DIR))

from inject_resolve import (  # noqa: E402
    resolve_inject_params,
    validate_benchmark_case,
)


@pytest.mark.parametrize("topo_size", ["s", "m", "l"])
@pytest.mark.parametrize("scenario", ["dc_clos_bgp", "dc_clos_service"])
def test_bgp_missing_route_advertisement_targets_advertisers(
    scenario: str, topo_size: str
) -> None:
    inject = resolve_inject_params(
        "bgp_missing_route_advertisement", scenario, topo_size, seed=43
    )
    host = inject["host_name"]
    assert "leaf" in host
    validate_benchmark_case(
        scenario, "bgp_missing_route_advertisement", inject, topo_size
    )


def test_bgp_missing_route_advertisement_rejects_spine_target() -> None:
    with pytest.raises(ValueError, match="leaf router"):
        validate_benchmark_case(
            "dc_clos_service",
            "bgp_missing_route_advertisement",
            {"host_name": "spine_router_2_2"},
            "l",
        )


def test_bgp_missing_route_advertisement_rejects_dc_clos_bgp_super_spine() -> None:
    with pytest.raises(ValueError, match="leaf router"):
        validate_benchmark_case(
            "dc_clos_bgp",
            "bgp_missing_route_advertisement",
            {"host_name": "super_spine_router_0"},
            "s",
        )


def test_bgp_missing_route_advertisement_simple_bgp_unchanged() -> None:
    inject = resolve_inject_params(
        "bgp_missing_route_advertisement", "simple_bgp", "", seed=42
    )
    assert inject["host_name"] in {"router1", "router2"}
    validate_benchmark_case("simple_bgp", "bgp_missing_route_advertisement", inject, "")


@pytest.mark.parametrize("topo_size", ["s", "m", "l"])
@pytest.mark.parametrize("scenario", ["dc_clos_bgp", "dc_clos_service"])
@pytest.mark.parametrize(
    "problem",
    ["host_static_blackhole", "bgp_blackhole_route_leak", "bgp_hijacking"],
)
def test_victim_host_problems_target_leaf_routers(
    scenario: str, topo_size: str, problem: str
) -> None:
    inject = resolve_inject_params(problem, scenario, topo_size, seed=43)
    assert "leaf" in inject["host_name"], inject
    validate_benchmark_case(scenario, problem, inject, topo_size)


def test_host_static_blackhole_rejects_spine_target() -> None:
    with pytest.raises(ValueError, match="leaf router"):
        validate_benchmark_case(
            "dc_clos_service",
            "host_static_blackhole",
            {"host_name": "spine_router_3_0"},
            "l",
        )


@pytest.mark.parametrize("topo_size", ["s", "m", "l"])
def test_host_vpn_membership_missing_targets_wireguard_peers(topo_size: str) -> None:
    inject = resolve_inject_params(
        "host_vpn_membership_missing",
        "rip_small_internet_vpn",
        topo_size,
        seed=43,
    )
    assert inject["host_name"] in {"pc1", "web_server_1_1", "web_server_1_2"}
    assert inject["host_name_2"] == "vpn_server_1"
    validate_benchmark_case(
        "rip_small_internet_vpn",
        "host_vpn_membership_missing",
        inject,
        topo_size,
    )


def test_host_vpn_membership_missing_rejects_non_peer_web_server() -> None:
    with pytest.raises(ValueError, match="WireGuard peer"):
        validate_benchmark_case(
            "rip_small_internet_vpn",
            "host_vpn_membership_missing",
            {"host_name": "web_server_4_3", "host_name_2": "vpn_server_1"},
            "l",
        )


@pytest.mark.parametrize(
    "yaml_name",
    [
        "releases/0.1.0/dev.yaml",
        "releases/0.1.0/test.yaml",
        "benchmark_selected.yaml",
    ],
)
def test_bundled_benchmark_yaml_cases_validate(yaml_name: str) -> None:
    import yaml

    path = _BENCHMARK_DIR / yaml_name
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for row in data.get("cases") or []:
        topo = row.get("topo_size") or ""
        if topo is None:
            topo = ""
        validate_benchmark_case(
            row["scenario"],
            row["problem"],
            dict(row.get("inject") or {}),
            str(topo),
        )
