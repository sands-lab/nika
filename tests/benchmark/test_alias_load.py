"""Benchmark rows accept canonical scenarios only."""

from __future__ import annotations

import pytest

from nika.workflows.benchmark.load_config import normalize_benchmark_row


@pytest.mark.parametrize(
    "scenario", ["dc_clos_service", "ospf_enterprise_dhcp", "p4_counter"]
)
def test_legacy_scenario_is_rejected(scenario: str) -> None:
    with pytest.raises(ValueError):
        normalize_benchmark_row(
            {
                "scenario": scenario,
                "problem": "link_down",
                "topo_size": "s",
                "inject": {"host_name": "x"},
            }
        )


def test_workload_column_is_rejected() -> None:
    with pytest.raises(ValueError, match="workload"):
        normalize_benchmark_row(
            {
                "scenario": "dc_clos",
                "problem": "link_down",
                "topo_size": "s",
                "workload": "service",
                "inject": {"host_name": "x"},
            }
        )
