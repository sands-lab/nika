from __future__ import annotations

from pathlib import Path

import yaml

from nika.problems.rca import canonical_root_causes
from nika.problems.rca.inventory import load_offline_net_env
from nika.problems.rca.materialize import ground_truth_for_case
from nika.workflows.benchmark.candidate_context import collapse_candidates, pool_context_key
from nika.workflows.benchmark.load_config import load_candidate_catalog
from nika.workflows.benchmark.pool_audit import audit_candidate_pool


def _link_down_case(host_name: str, intf_name: str = "eth0") -> dict:
    net_env = load_offline_net_env("dc_clos", "s")
    inject = {"host_name": host_name, "intf_name": intf_name}
    truth = ground_truth_for_case(
        problem="link_down",
        params=inject,
        scenario="dc_clos",
        topo_size="s",
        net_env=net_env,
    )
    return {
        "topo_size": "s",
        "inject": inject,
        "root_causes": canonical_root_causes(truth.root_causes),
    }


def _write_mini_catalog(tmp_path: Path) -> Path:
    pool_dir = tmp_path / "pool"
    candidate = pool_dir / "dc_clos" / "link_down.yaml"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text(
        yaml.safe_dump(
            {
                "scenario": {"name": "dc_clos"},
                "failure": {
                    "fault_type": "link_down",
                    "cases": [
                        _link_down_case("client_0"),
                        _link_down_case("super_spine_router_0", "eth2"),
                    ],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    healthy = pool_dir / "dc_clos" / "healthy.yaml"
    healthy.write_text(
        yaml.safe_dump(
            {
                "scenario": {"name": "dc_clos"},
                "healthy": {"variants": [{"topo_size": "s"}]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return pool_dir


def test_collapse_candidates_keeps_one_per_context(tmp_path: Path) -> None:
    pool_dir = _write_mini_catalog(tmp_path)
    rows = load_candidate_catalog(pool_dir)
    collapsed = collapse_candidates(rows)
    failure_rows = [row for row in collapsed if row["problem"] == "link_down"]
    assert len(failure_rows) == 1
    assert failure_rows[0]["inject"]["host_name"] == "client_0"


def test_audit_mini_catalog_passes(tmp_path: Path) -> None:
    pool_dir = _write_mini_catalog(tmp_path)
    report = audit_candidate_pool(str(pool_dir))
    assert report["summary"]["total_rows"] == 3
    assert report["summary"]["collapsed_contexts"] == 2
    assert report["eligible_for_selection"] is True


def test_pool_context_key_distinguishes_inject_variants(tmp_path: Path) -> None:
    pool_dir = _write_mini_catalog(tmp_path)
    rows = load_candidate_catalog(pool_dir)
    failure_rows = [row for row in rows if row["problem"] == "link_down"]
    assert pool_context_key(failure_rows[0]) == pool_context_key(failure_rows[1])
