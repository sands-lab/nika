from __future__ import annotations

from pathlib import Path

import yaml

from nika.workflows.benchmark.inject_enumerate import enumerate_inject_params
from nika.workflows.benchmark.inject_resolve import validate_benchmark_case
from nika.config import BENCHMARK_DIR
from nika.problems.rca.inventory import load_offline_net_env
from nika.problems.rca.materialize import ground_truth_for_case
from nika.workflows.benchmark.load_config import (
    load_benchmark_input,
    load_candidate_catalog,
    normalize_benchmark_row,
)
from nika.workflows.benchmark.candidate_context import deployment_environment_key
from nika.workflows.benchmark.healthy import HEALTHY_PROBLEM
from nika.workflows.benchmark.resume import benchmark_option_id


def _write_catalog(tmp_path: Path, *, cases: list[dict]) -> Path:
    pool_dir = tmp_path / "pool"
    candidate_path = pool_dir / "dc_clos" / "link_down.yaml"
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(
        yaml.safe_dump(
            {
                "scenario": {"name": "dc_clos"},
                "failure": {
                    "fault_type": "link_down",
                    "cases": cases,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return pool_dir


def test_candidate_catalog_loads_flat_case(tmp_path: Path) -> None:
    net_env = load_offline_net_env("dc_clos", "s")
    inject = {"host_name": "client_0", "intf_name": "eth0"}
    truth = ground_truth_for_case(
        problem="link_down",
        params=inject,
        scenario="dc_clos",
        topo_size="s",
        net_env=net_env,
    )
    row = normalize_benchmark_row(
        {
            "problem": "link_down",
            "scenario": "dc_clos",
            "topo_size": "s",
            "inject": inject,
            "root_causes": [
                {
                    "resource": root_cause.resource.model_dump(
                        mode="json", exclude_none=True
                    ),
                    "fault_type": root_cause.fault_type,
                }
                for root_cause in truth.root_causes
            ],
        }
    )
    cases = [
        {
            "topo_size": "s",
            "inject": inject,
            "root_causes": [
                {
                    "resource": root_cause.resource.model_dump(
                        mode="json", exclude_none=True, exclude={"id"}
                    ),
                    "fault_type": root_cause.fault_type,
                }
                for root_cause in truth.root_causes
            ],
        }
    ]
    option_id = benchmark_option_id(row)
    pool_dir = _write_catalog(tmp_path, cases=cases)

    loaded = load_candidate_catalog(pool_dir)
    assert len(loaded) == 1
    assert loaded[0]["candidate_option_id"] == option_id
    assert loaded[0]["inject"] == row["inject"]
    assert (
        loaded[0]["root_causes"][0]["resource"]
        == cases[0]["root_causes"][0]["resource"]
    )


def test_candidate_catalog_rejects_list_valued_inject(tmp_path: Path) -> None:
    pool_dir = tmp_path / "pool"
    candidate_path = pool_dir / "campus_lan" / "arp_acl_block.yaml"
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(
        yaml.safe_dump(
            {
                "scenario": {"name": "campus_lan"},
                "failure": {
                    "fault_type": "arp_acl_block",
                    "cases": [
                        {
                            "topo_size": "s",
                            "inject": {
                                "host_name": ["pc_1_1_1_1", "pc_2_1_1_1"],
                            },
                            "root_causes": [
                                {
                                    "resource": {
                                        "kind": "node",
                                        "node": "pc_1_1_1_1",
                                    },
                                    "fault_type": "arp_acl_block",
                                }
                            ],
                        }
                    ],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    try:
        load_candidate_catalog(pool_dir)
    except ValueError as exc:
        assert "scalar values" in str(exc)
    else:
        raise AssertionError("expected list-valued inject to be rejected")


def test_link_down_enumerates_distinct_topology_links() -> None:
    net_env = load_offline_net_env("dc_clos", "s")
    options = enumerate_inject_params("link_down", "dc_clos", "s", net_env=net_env)
    targets = {(row["host_name"], row["intf_name"]) for row in options}
    assert len(targets) > 1
    resources = set()
    for inject in options:
        validate_benchmark_case("dc_clos", "link_down", inject, "s", net_env=net_env)
        truth = ground_truth_for_case(
            problem="link_down",
            params=inject,
            scenario="dc_clos",
            topo_size="s",
            net_env=net_env,
        )
        resources.add(truth.root_causes[0].resource.id)
    assert len(resources) == len(options)
    assert "link/leaf_router_0_0:eth0--spine_router_0_0:eth1" in resources


def test_isp_link_variants_recompute_symptom_targets() -> None:
    net_env = load_offline_net_env(
        "isp_janos-us", igp="isis", bgp_mode="none", rpki=False
    )
    options = enumerate_inject_params(
        "link_packet_corruption",
        "isp_janos-us",
        "m",
        isp_options={"igp": "isis", "bgp_mode": "none", "rpki": "false"},
        net_env=net_env,
    )
    assert len(options) > 1
    symptoms = {(row["symptom_host"], row["peer_host"]) for row in options}
    assert len(symptoms) > 1
    albany = next(
        row
        for row in options
        if row["host_name"] == "albany" and row["intf_name"] == "eth0"
    )
    assert albany["symptom_host"] == "pc_albany"
    assert albany["peer_host"] == "pc_cleveland"
    assert albany["probe_dst_ip"].startswith("10.254.")


def test_link_family_problems_share_inject_targets() -> None:
    net_env = load_offline_net_env("campus_lan", "s")
    baseline = enumerate_inject_params("link_down", "campus_lan", "s", net_env=net_env)
    baseline_targets = {(row["host_name"], row["intf_name"]) for row in baseline}
    assert len(baseline_targets) > 1
    assert ("backend_web_0", "eth0") in baseline_targets

    detach = enumerate_inject_params("link_detach", "campus_lan", "s", net_env=net_env)
    detach_targets = {(row["host_name"], row["intf_name"]) for row in detach}
    assert detach_targets < baseline_targets
    assert ("backend_web_0", "eth0") not in detach_targets
    assert ("pc_1_1_1_1", "eth0") in detach_targets

    for problem in (
        "link_flap",
        "link_packet_corruption",
        "link_capacity_bottleneck",
    ):
        options = enumerate_inject_params(problem, "campus_lan", "s", net_env=net_env)
        targets = {(row["host_name"], row["intf_name"]) for row in options}
        # VDE proxy inject requires point-to-point LANs only.
        assert targets < baseline_targets
        assert ("backend_web_0", "eth0") not in targets
        assert len(targets) > 1
        for inject in options:
            validate_benchmark_case("campus_lan", problem, inject, "s", net_env=net_env)


def test_bundled_candidate_catalog_loads_all_options() -> None:
    pool = BENCHMARK_DIR / "working" / "pool"
    assert pool.is_dir()
    candidate_files = sorted(pool.rglob("*.yaml"))
    assert candidate_files
    assert not any("iosxr_simple_bgp" in path.as_posix() for path in candidate_files)
    failure_text = next(
        path for path in candidate_files if path.name == "link_down.yaml"
    ).read_text(encoding="utf-8")
    assert "case_type:" not in failure_text
    assert "catalog_version:" not in failure_text
    assert "option_id:" not in failure_text
    assert "fault_type: link_down" in failure_text
    assert "cases:" in failure_text
    assert "variants:" not in failure_text
    assert "resources:" not in failure_text
    rows = load_benchmark_input(pool)
    assert rows
    assert not any(row["scenario"] == "iosxr_simple_bgp" for row in rows)
    assert len({row["candidate_option_id"] for row in rows}) == len(rows)
    healthy_environments = {
        deployment_environment_key(row)
        for row in rows
        if row["problem"] == HEALTHY_PROBLEM
    }
    assert all(
        deployment_environment_key(row) in healthy_environments
        for row in rows
        if row["problem"] != HEALTHY_PROBLEM
    )
