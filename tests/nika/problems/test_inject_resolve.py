"""Offline inject-target resolution for benchmark YAML generation."""

from __future__ import annotations

import sys

import pytest

from nika.config import BENCHMARK_DIR

if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

from inject_resolve import (  # noqa: E402
    resolve_inject_params,
    validate_benchmark_case,
)


@pytest.mark.parametrize(
    ("scenario", "target", "attacker", "observer", "url"),
    [
        (
            "dc_clos",
            "webserver0_pod0",
            "client_0",
            "dns_pod0",
            "http://10.0.1.2/small.bin",
        ),
        (
            "campus_lan",
            "web_server_0",
            "pc_1_1_1_1",
            "pc_2_1_1_1",
            "http://10.200.0.3/",
        ),
        (
            "enterprise_branch",
            "hq_srv",
            "br1_corp_pc",
            "hq_corp_pc",
            "http://10.0.20.2/small.bin",
        ),
        ("sdn_l3_clos", "web_2", "client_4_1", "client_1_1", "http://10.0.2.11/"),
        ("p4_dc_fabric", "web_2", "client_4_1", "client_1_1", "http://10.0.2.11/"),
        ("p4_dc_gateway", "service_1_1", "client_2", "client_1", "http://20.0.0.1:80/"),
    ],
)
def test_web_dos_resolves_one_independently_observed_http_path(
    scenario: str,
    target: str,
    attacker: str,
    observer: str,
    url: str,
) -> None:
    inject = resolve_inject_params("web_dos_attack", scenario, "s", seed=42)

    expected = {
        "host_name": target,
        "attacker_device": attacker,
        "observer_device": observer,
        "probe_url": url,
    }
    if scenario == "p4_dc_gateway":
        expected["attack_url"] = url
    elif scenario == "enterprise_branch":
        expected["attack_url"] = "http://10.0.20.2/nika-dos-dir/"
    assert inject == expected
    assert len({target, attacker, observer}) == 3


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
    from nika.problems.registry import get_problem_class, resolve_problem_name
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


def test_mtu_mismatch_resolves_to_intermediate_router() -> None:
    from nika.problems.registry import get_problem_class

    params = resolve_inject_params("mtu_mismatch", "dc_clos", "s", seed=1)
    assert params["mtu"] == "500"
    assert "intf_name" in params
    assert params["host_name"].startswith(
        ("leaf_router_", "spine_router_", "super_spine_router_")
    )
    cls = get_problem_class("mtu_mismatch")
    assert cls is not None
    assert "dc_clos" in (cls.COMPATIBLE_COLUMNS or set())
    assert "p4_dc_gateway" not in (cls.COMPATIBLE_COLUMNS or set())


def test_icmp_frag_needed_filter_resolves_on_dc_clos() -> None:
    params = resolve_inject_params(
        "icmp_frag_needed_filter_misconfiguration", "dc_clos", "s", seed=1
    )
    assert "host_name" in params
    assert not params["host_name"].startswith("gateway_")


def test_host_vpn_alias_resolves_to_wireguard() -> None:
    from nika.problems.registry import get_problem_class, resolve_problem_name

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


def test_host_ip_conflict_distinct_hosts_dc_clos() -> None:
    inject = resolve_inject_params("host_ip_conflict", "dc_clos", "s", seed=42)
    assert inject["host_name"] != inject["host_name_2"], inject
    validate_benchmark_case("dc_clos", "host_ip_conflict", inject, "s")


def test_mac_address_conflict_prefers_endpoint_hosts() -> None:
    inject = resolve_inject_params("mac_address_conflict", "dc_clos", "s", seed=7)
    assert inject["host_name"] != inject["host_name_2"], inject
    for name in (inject["host_name"], inject["host_name_2"]):
        assert not str(name).startswith(("spine", "super_spine")), inject
    validate_benchmark_case("dc_clos", "mac_address_conflict", inject, "s")


@pytest.mark.parametrize("yaml_name", ["benchmark_selected.yaml"])
def test_bundled_benchmark_yaml_cases_validate(yaml_name: str) -> None:
    from nika.workflows.benchmark.healthy import is_healthy_case
    from nika.workflows.benchmark.load_config import load_benchmark_yaml
    from nika.workflows.benchmark.isp_options import isp_options_from_row

    path = BENCHMARK_DIR / yaml_name
    for row in load_benchmark_yaml(path):
        if is_healthy_case(row["problem"]):
            continue
        validate_benchmark_case(
            row["scenario"],
            row["problem"],
            dict(row.get("inject") or {}),
            str(row.get("topo_size") or ""),
            isp_options=isp_options_from_row(row),
        )
