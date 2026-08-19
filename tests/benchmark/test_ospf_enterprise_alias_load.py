"""Benchmark YAML load normalizes legacy campus LAN scenario ids."""

from __future__ import annotations

from pathlib import Path

from nika.workflows.benchmark.load_config import (
    load_benchmark_yaml,
    normalize_benchmark_row,
)


def test_normalize_legacy_ospf_enterprise_dhcp() -> None:
    row = normalize_benchmark_row(
        {
            "scenario": "ospf_enterprise_dhcp",
            "problem": "dhcp_service_down",
            "topo_size": "s",
            "inject": {"host_name": "dhcp_server"},
        }
    )
    assert row["scenario"] == "campus_lan"
    assert row["workload"] == "dhcp"
    assert row["problem"] == "dhcp_service_down"


def test_normalize_legacy_ospf_enterprise_static() -> None:
    row = normalize_benchmark_row(
        {
            "scenario": "ospf_enterprise_static",
            "problem": "host_incorrect_ip",
            "topo_size": "s",
            "inject": {"host_name": "pc_1_1_1_1"},
        }
    )
    assert row["scenario"] == "campus_lan"
    assert row["workload"] == "static"


def test_load_release_case_with_legacy_scenario(tmp_path: Path) -> None:
    path = tmp_path / "cases.yaml"
    path.write_text(
        """
seed: 42
cases:
  - scenario: ospf_enterprise_dhcp
    topo_size: s
    problem: dhcp_service_down
    inject:
      host_name: dhcp_server
""",
        encoding="utf-8",
    )
    rows = load_benchmark_yaml(path)
    assert rows[0]["scenario"] == "campus_lan"
    assert rows[0]["workload"] == "dhcp"
