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
def test_bgp_missing_route_advertisement_targets_advertisers(
    topo_size: str,
) -> None:
    inject = resolve_inject_params(
        "bgp_missing_route_advertisement",
        "dc_clos",
        topo_size,
        seed=43,
    )
    host = inject["host_name"]
    assert "leaf" in host
    validate_benchmark_case(
        "dc_clos",
        "bgp_missing_route_advertisement",
        inject,
        topo_size,
    )


def test_bgp_missing_route_advertisement_rejects_spine_target() -> None:
    with pytest.raises(ValueError, match="leaf router"):
        validate_benchmark_case(
            "dc_clos",
            "bgp_missing_route_advertisement",
            {"host_name": "spine_router_2_2"},
            "l",
        )


def test_bgp_missing_route_advertisement_rejects_dc_clos_super_spine() -> None:
    with pytest.raises(ValueError, match="leaf router"):
        validate_benchmark_case(
            "dc_clos",
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
@pytest.mark.parametrize(
    "problem",
    ["host_static_blackhole", "bgp_blackhole_route_leak", "bgp_hijacking"],
)
def test_victim_host_problems_target_leaf_routers(topo_size: str, problem: str) -> None:
    inject = resolve_inject_params(problem, "dc_clos", topo_size, seed=43)
    assert "leaf" in inject["host_name"], inject
    validate_benchmark_case("dc_clos", problem, inject, topo_size)


def test_host_static_blackhole_rejects_spine_target() -> None:
    with pytest.raises(ValueError, match="leaf router"):
        validate_benchmark_case(
            "dc_clos",
            "host_static_blackhole",
            {"host_name": "spine_router_3_0"},
            "l",
        )


@pytest.mark.parametrize("topo_size", ["s", "m", "l"])
def test_wireguard_peer_key_targets_primary_hq_tunnels(topo_size: str) -> None:
    from nika.net_env.enterprise_branch.topology import (
        primary_hq_peer_targets,
    )

    inject = resolve_inject_params(
        "wireguard_peer_key_misconfiguration",
        "enterprise_branch",
        topo_size,
        seed=42,
    )
    eligible = set(primary_hq_peer_targets(topo_size))  # type: ignore[arg-type]
    assert (inject["host_name"], inject["intf_name"]) in eligible
    validate_benchmark_case(
        "enterprise_branch",
        "wireguard_peer_key_misconfiguration",
        inject,
        topo_size,
    )


def test_wireguard_peer_key_rejects_non_primary_iface() -> None:
    with pytest.raises(ValueError, match="primary HQ tunnel"):
        validate_benchmark_case(
            "enterprise_branch",
            "wireguard_peer_key_misconfiguration",
            {"host_name": "br1_edge", "intf_name": "wg_hq_b"},
            "s",
        )


def test_wireguard_peer_key_accepts_dual_homed_primary() -> None:
    validate_benchmark_case(
        "enterprise_branch",
        "wireguard_peer_key_misconfiguration",
        {"host_name": "br1_edge", "intf_name": "wg_hq"},
        "s",
    )


def test_link_fragmentation_disabled_alias_resolves_to_mtu_mismatch() -> None:
    from nika.problems.prob_pool import get_problem_class, resolve_problem_name
    from nika.workflows.benchmark.load_config import normalize_benchmark_row

    assert resolve_problem_name("link_fragmentation_disabled") == "mtu_mismatch"
    cls = get_problem_class("link_fragmentation_disabled")
    assert cls is not None
    assert cls.root_cause_name == "mtu_mismatch"

    row = normalize_benchmark_row(
        {
            "scenario": "dc_clos",
            "topo_size": "",
            "problem": "link_fragmentation_disabled",
            "inject": {"host_name": "pc1", "mtu": "100"},
            "root_causes": [
                {
                    "resource": {
                        "kind": "interface",
                        "node": "pc1",
                        "name": "eth0",
                    },
                    "fault_type": "link_fragmentation_disabled",
                }
            ],
        }
    )
    assert row["problem"] == "mtu_mismatch"
    assert row["root_causes"][0]["fault_type"] == "mtu_mismatch"


def test_host_vpn_alias_resolves_to_wireguard() -> None:
    from nika.problems.prob_pool import get_problem_class, resolve_problem_name

    assert (
        resolve_problem_name("host_vpn_membership_missing")
        == "wireguard_peer_key_misconfiguration"
    )
    cls = get_problem_class("host_vpn_membership_missing")
    assert cls is not None
    assert cls.root_cause_name == "wireguard_peer_key_misconfiguration"


def test_legacy_scenario_benchmark_row_is_rejected() -> None:
    from nika.workflows.benchmark.load_config import normalize_benchmark_row

    with pytest.raises(ValueError, match="not found in the pool"):
        normalize_benchmark_row(
            {
                "scenario": "rip_small_internet_vpn",
                "topo_size": "s",
                "problem": "host_vpn_membership_missing",
                "inject": {
                    "host_name": "web_server_1_1",
                    "host_name_2": "vpn_server_1",
                },
                "root_causes": [
                    {
                        "resource": {"kind": "node", "node": "vpn_server_1"},
                        "fault_type": "host_vpn_membership_missing",
                    }
                ],
            }
        )


def test_selected_scenario_mapping_includes_wireguard() -> None:
    from generate_benchmark import SELECTED_SCENARIO_FOR_PROBLEM

    assert (
        SELECTED_SCENARIO_FOR_PROBLEM["wireguard_peer_key_misconfiguration"]
        == "enterprise_branch"
    )
    assert (
        SELECTED_SCENARIO_FOR_PROBLEM["wireguard_allowed_ips_misconfiguration"]
        == "enterprise_branch"
    )
    assert SELECTED_SCENARIO_FOR_PROBLEM["vrf_dscp_remarking"] == "enterprise_branch"
    assert SELECTED_SCENARIO_FOR_PROBLEM["p4_action_selector_member_misconfig"] == (
        "p4_dc_fabric"
    )
    assert SELECTED_SCENARIO_FOR_PROBLEM["p4_table_resource_exhaustion"] == (
        "p4_dc_fabric"
    )
    assert SELECTED_SCENARIO_FOR_PROBLEM["bgp_rpki_invalid_route_leak"] == "isp"


@pytest.mark.parametrize("topo_size", ["s", "m", "l"])
@pytest.mark.parametrize(
    "scenario", ["dc_clos", "campus_lan", "enterprise_branch", "sdn_l3_clos"]
)
def test_device_forwarding_corruption_targets_a_forwarding_node(
    scenario: str, topo_size: str
) -> None:
    inject = resolve_inject_params(
        "device_forwarding_packet_corruption", scenario, topo_size, seed=43
    )
    assert inject["forwarding_device"]
    assert inject["intf_name"].startswith("eth")
    assert inject["seed"] == "43"
    validate_benchmark_case(
        scenario, "device_forwarding_packet_corruption", inject, topo_size
    )


def test_p4_dc_fabric_runtime_failures_target_a_leaf() -> None:
    for problem in (
        "p4_action_selector_member_misconfig",
        "p4_ecmp_group_member_missing",
        "p4runtime_pipeline_mismatch",
        "p4runtime_partial_write",
        "p4_table_resource_exhaustion",
    ):
        inject = resolve_inject_params(problem, "p4_dc_fabric", "s", seed=0)
        assert inject["host_name"].startswith("leaf_"), problem


def test_p4_dc_fabric_corruption_pins_client_eth0() -> None:
    inject = resolve_inject_params(
        "link_packet_corruption", "p4_dc_fabric", "s", seed=0
    )
    assert "client" in inject["host_name"]
    assert inject["intf_name"] == "eth0"


def test_vrf_dscp_remarking_inject_resolve_and_validate() -> None:
    """One offline resolve+validate check; Docker inject covers runtime behavior."""
    from nika.net_env.enterprise_branch.topology import (
        dscp_remark_inject_targets,
    )

    inject = resolve_inject_params(
        "vrf_dscp_remarking",
        "enterprise_branch",
        "s",
        seed=42,
    )
    eligible = {
        (t.edge, t.intf_name, t.src_host, t.dst_host)
        for t in dscp_remark_inject_targets("s")
    }
    key = (
        inject["host_name"],
        inject["intf_name"],
        inject["src_host"],
        inject["dst_host"],
    )
    assert key in eligible
    validate_benchmark_case(
        "enterprise_branch",
        "vrf_dscp_remarking",
        inject,
        "s",
    )
    with pytest.raises(ValueError, match="eligible"):
        validate_benchmark_case(
            "enterprise_branch",
            "vrf_dscp_remarking",
            {
                "host_name": "br1_edge",
                "intf_name": "wg_hq_b",
                "src_host": "br1_corp_pc",
                "dst_host": "hq_corp_pc",
            },
            "s",
        )


@pytest.mark.parametrize("topo_size", ["s", "m", "l"])
def test_wireguard_allowed_ips_targets_primary_hq_and_remote_prefix(
    topo_size: str,
) -> None:
    from nika.net_env.enterprise_branch.topology import (
        primary_hq_peer_targets,
        remote_advertised_prefixes_for_spoke,
    )

    inject = resolve_inject_params(
        "wireguard_allowed_ips_misconfiguration",
        "enterprise_branch",
        topo_size,
        seed=42,
    )
    eligible = set(primary_hq_peer_targets(topo_size))  # type: ignore[arg-type]
    assert (inject["host_name"], inject["intf_name"]) in eligible
    spoke = inject["host_name"].removesuffix("_edge")
    remotes = remote_advertised_prefixes_for_spoke(topo_size, spoke)  # type: ignore[arg-type]
    assert inject["target_prefix"] in remotes
    validate_benchmark_case(
        "enterprise_branch",
        "wireguard_allowed_ips_misconfiguration",
        inject,
        topo_size,
    )


def test_wireguard_allowed_ips_rejects_non_hq_iface() -> None:
    with pytest.raises(ValueError, match="primary HQ tunnel"):
        validate_benchmark_case(
            "enterprise_branch",
            "wireguard_allowed_ips_misconfiguration",
            {
                "host_name": "br1_edge",
                "intf_name": "wg_hq_b",
                "target_prefix": "10.0.20.0/24",
            },
            "m",
        )


def test_wireguard_allowed_ips_accepts_dual_homed_primary() -> None:
    validate_benchmark_case(
        "enterprise_branch",
        "wireguard_allowed_ips_misconfiguration",
        {
            "host_name": "br1_edge",
            "intf_name": "wg_hq",
            "target_prefix": "10.0.20.0/24",
        },
        "m",
    )


def test_wireguard_allowed_ips_rejects_local_prefix() -> None:
    with pytest.raises(ValueError, match="remote advertised prefix"):
        validate_benchmark_case(
            "enterprise_branch",
            "wireguard_allowed_ips_misconfiguration",
            {
                "host_name": "br1_edge",
                "intf_name": "wg_hq",
                "target_prefix": "10.1.10.0/24",
            },
            "s",
        )


@pytest.mark.parametrize("yaml_name", ["benchmark_selected.yaml"])
def test_bundled_benchmark_yaml_cases_validate(yaml_name: str) -> None:
    from nika.workflows.benchmark.load_config import load_benchmark_yaml
    from nika.workflows.benchmark.isp_options import isp_options_from_row

    path = _BENCHMARK_DIR / yaml_name
    for row in load_benchmark_yaml(path):
        validate_benchmark_case(
            row["scenario"],
            row["problem"],
            dict(row.get("inject") or {}),
            str(row.get("topo_size") or ""),
            isp_options=isp_options_from_row(row),
        )
