"""Healthy (no-fault) benchmark case loading and generation."""

from __future__ import annotations

import pytest

from nika.config import BENCHMARK_DIR
from nika.workflows.benchmark.healthy import (
    HEALTHY_PROBLEM,
    is_healthy_case,
)
from nika.workflows.benchmark.load_config import (
    load_benchmark_input,
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
                "scenario": "isp_abilene",
                "topo_size": "s",
                "problem": HEALTHY_PROBLEM,
                "inject": {},
            }
        )


def test_candidate_catalog_includes_healthy_scenarios() -> None:
    pool = BENCHMARK_DIR / "working" / "pool"
    rows = load_benchmark_input(pool)
    healthy = [row for row in rows if is_healthy_case(row["problem"])]
    scenarios = {row["scenario"] for row in healthy}
    assert "campus_lan" in scenarios
    assert "dc_clos" in scenarios
    assert "enterprise_branch" in scenarios
    assert "isp_abilene" in scenarios
    assert "isp_abilene_ebgp_rtbh" in scenarios
    assert "isp_abilene_ebgp_rpki" in scenarios
    assert "k8s_lab" in scenarios
    assert "llmd_lab" in scenarios
    assert "min3clos" in scenarios
    assert "p4_dc_fabric" in scenarios
    assert "p4_dc_gateway" in scenarios
    assert "sdn_l3_clos" in scenarios
    assert "isp" not in scenarios
    assert "isp_ebgp_rtbh" not in scenarios
    assert not (pool / "isp").is_dir()
    assert not (pool / "isp_ebgp_rtbh").is_dir()
    for row in healthy:
        assert row["inject"] == {}
        assert row["root_causes"] == []
