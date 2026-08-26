"""Healthy (no-fault) benchmark case loading and generation."""

from __future__ import annotations

import pytest

from nika.config import BENCHMARK_DIR
from nika.workflows.benchmark.healthy import (
    HEALTHY_PROBLEM,
    SELECTED_HEALTHY_SCENARIOS,
    is_healthy_case,
)
from nika.workflows.benchmark.load_config import (
    load_benchmark_yaml,
    normalize_benchmark_row,
)


def test_normalize_healthy_row() -> None:
    row = normalize_benchmark_row(
        {
            "scenario": "campus_lan",
            "topo_size": "s",
            "problem": HEALTHY_PROBLEM,
            "inject": {},
        }
    )
    assert row["problem"] == HEALTHY_PROBLEM
    assert row["inject"] == {}
    assert row["root_causes"] == []
    assert is_healthy_case(row["problem"])


def test_normalize_healthy_rejects_root_causes() -> None:
    with pytest.raises(ValueError, match="root_causes"):
        normalize_benchmark_row(
            {
                "scenario": "campus_lan",
                "topo_size": "s",
                "problem": HEALTHY_PROBLEM,
                "inject": {},
                "root_causes": [
                    {
                        "resource": {"kind": "node", "node": "pc1"},
                        "fault_type": "link_down",
                    }
                ],
            }
        )


def test_normalize_healthy_rejects_inject_params() -> None:
    with pytest.raises(ValueError, match="empty 'inject'"):
        normalize_benchmark_row(
            {
                "scenario": "campus_lan",
                "topo_size": "s",
                "problem": HEALTHY_PROBLEM,
                "inject": {"host_name": "pc1"},
            }
        )


def test_normalize_healthy_isp_requires_profile() -> None:
    with pytest.raises(ValueError, match="explicit deploy options"):
        normalize_benchmark_row(
            {
                "scenario": "isp",
                "topo_size": "s",
                "problem": HEALTHY_PROBLEM,
                "inject": {},
            }
        )


def test_selected_yaml_includes_one_healthy_per_scenario() -> None:
    rows = load_benchmark_yaml(BENCHMARK_DIR / "benchmark_selected.yaml")
    healthy = [row for row in rows if is_healthy_case(row["problem"])]
    assert len(healthy) == len(SELECTED_HEALTHY_SCENARIOS)
    assert {row["scenario"] for row in healthy} == set(SELECTED_HEALTHY_SCENARIOS)
    for row in healthy:
        assert row["inject"] == {}
        assert row["root_causes"] == []
