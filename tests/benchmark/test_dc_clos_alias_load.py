"""Benchmark YAML load normalizes legacy Clos scenario ids."""

from __future__ import annotations

from pathlib import Path

from nika.workflows.benchmark.load_config import (
    load_benchmark_yaml,
    normalize_benchmark_row,
)


def test_normalize_legacy_dc_clos_service() -> None:
    row = normalize_benchmark_row(
        {
            "scenario": "dc_clos_service",
            "problem": "dns_service_down",
            "topo_size": "s",
            "inject": {"host_name": "dns_pod0"},
        }
    )
    assert row["scenario"] == "dc_clos"
    assert row["workload"] == "service"
    assert row["problem"] == "dns_service_down"


def test_normalize_legacy_dc_clos_bgp() -> None:
    row = normalize_benchmark_row(
        {
            "scenario": "dc_clos_bgp",
            "problem": "link_down",
            "topo_size": "s",
            "inject": {"host_name": "pc_0_0", "intf_name": "eth0"},
        }
    )
    assert row["scenario"] == "dc_clos"
    assert row["workload"] == "host"


def test_load_release_case_with_legacy_scenario(tmp_path: Path) -> None:
    path = tmp_path / "cases.yaml"
    path.write_text(
        """
seed: 42
cases:
  - scenario: dc_clos_service
    topo_size: s
    problem: dns_service_down
    inject:
      host_name: dns_pod0
""",
        encoding="utf-8",
    )
    rows = load_benchmark_yaml(path)
    assert rows[0]["scenario"] == "dc_clos"
    assert rows[0]["workload"] == "service"
