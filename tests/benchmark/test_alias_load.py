"""Benchmark rows accept canonical scenarios only."""

from __future__ import annotations

import pytest

from nika.workflows.benchmark.load_config import normalize_benchmark_row

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("scenario", ["dc_clos_service", "ospf_enterprise_dhcp"])
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


@pytest.mark.parametrize(
    ("problem", "expected"),
    [
        ("link_fragmentation_disabled", "mtu_mismatch"),
        ("frr_service_down", "routing_service_down"),
    ],
)
def test_failure_aliases_use_current_ids(tmp_path, problem, expected):
    import yaml

    from nika.workflows.benchmark.load_config import load_benchmark_yaml

    path = tmp_path / "task.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "scenario": "dc_clos",
                "topo_size": "s",
                "problem": problem,
                "inject": {"host_name": "spine_1"},
                "root_causes": [
                    {
                        "resource": {"kind": "node", "node": "spine_1"},
                        "fault_type": problem,
                    }
                ],
            }
        )
    )
    row = load_benchmark_yaml(path)[0]
    assert row["problem"] == expected
    assert row["root_causes"][0]["fault_type"] == expected
