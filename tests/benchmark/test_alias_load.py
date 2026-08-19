"""Benchmark YAML load normalizes legacy scenario ids (Clos / campus LAN)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nika.workflows.benchmark.load_config import (
    load_benchmark_yaml,
    normalize_benchmark_row,
)

_LEGACY_ROWS = (
    pytest.param(
        {
            "scenario": "dc_clos_service",
            "problem": "dns_service_down",
            "topo_size": "s",
            "inject": {"host_name": "dns_pod0"},
        },
        {"scenario": "dc_clos", "workload": "service", "problem": "dns_service_down"},
        id="dc_clos_service",
    ),
    pytest.param(
        {
            "scenario": "dc_clos_bgp",
            "problem": "link_down",
            "topo_size": "s",
            "inject": {"host_name": "pc_0_0", "intf_name": "eth0"},
        },
        {"scenario": "dc_clos", "workload": "host", "problem": "link_down"},
        id="dc_clos_bgp",
    ),
    pytest.param(
        {
            "scenario": "ospf_enterprise_dhcp",
            "problem": "dhcp_service_down",
            "topo_size": "s",
            "inject": {"host_name": "dhcp_server"},
        },
        {
            "scenario": "campus_lan",
            "workload": "dhcp",
            "problem": "dhcp_service_down",
        },
        id="ospf_enterprise_dhcp",
    ),
    pytest.param(
        {
            "scenario": "ospf_enterprise_static",
            "problem": "host_incorrect_ip",
            "topo_size": "s",
            "inject": {"host_name": "pc_1_1_1_1"},
        },
        {
            "scenario": "campus_lan",
            "workload": "static",
            "problem": "host_incorrect_ip",
        },
        id="ospf_enterprise_static",
    ),
)


@pytest.mark.parametrize(("row", "expected"), _LEGACY_ROWS)
def test_normalize_legacy_scenario(row: dict, expected: dict) -> None:
    normalized = normalize_benchmark_row(row)
    for key, value in expected.items():
        assert normalized[key] == value


@pytest.mark.parametrize(
    ("scenario", "problem", "inject", "expected_scenario", "expected_workload"),
    (
        pytest.param(
            "dc_clos_service",
            "dns_service_down",
            {"host_name": "dns_pod0"},
            "dc_clos",
            "service",
            id="dc_clos_service",
        ),
        pytest.param(
            "ospf_enterprise_dhcp",
            "dhcp_service_down",
            {"host_name": "dhcp_server"},
            "campus_lan",
            "dhcp",
            id="ospf_enterprise_dhcp",
        ),
    ),
)
def test_load_release_case_with_legacy_scenario(
    tmp_path: Path,
    scenario: str,
    problem: str,
    inject: dict,
    expected_scenario: str,
    expected_workload: str,
) -> None:
    path = tmp_path / "cases.yaml"
    path.write_text(
        f"""
seed: 42
cases:
  - scenario: {scenario}
    topo_size: s
    problem: {problem}
    inject:
      host_name: {inject["host_name"]}
""",
        encoding="utf-8",
    )
    rows = load_benchmark_yaml(path)
    assert rows[0]["scenario"] == expected_scenario
    assert rows[0]["workload"] == expected_workload
