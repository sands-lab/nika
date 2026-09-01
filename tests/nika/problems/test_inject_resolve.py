"""Offline inject-target resolution for benchmark YAML generation."""

from __future__ import annotations

import pytest

from nika.config import BENCHMARK_DIR
from nika.workflows.benchmark.inject_enumerate import enumerate_inject_params
from nika.workflows.benchmark.inject_resolve import (
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


@pytest.mark.parametrize("seed", range(5))
def test_bgp_missing_route_advertisement_isp_targets_originator(seed: int) -> None:
    from nika.net_env.isp.bgp import compile_bgp_plan
    from nika.net_env.isp.igp import IspConfig, compile_isp_plan

    isp_opts = {
        "topo": "abilene",
        "igp": "ospf",
        "bgp_mode": "ebgp",
        "rpki": False,
    }
    inject = resolve_inject_params(
        "bgp_missing_route_advertisement",
        "isp_abilene",
        "",
        seed=seed,
        isp_options=isp_opts,
    )
    isp_plan = compile_isp_plan(IspConfig(topology="abilene", igp="ospf"))
    bgp = compile_bgp_plan(isp_plan, "ebgp")
    assert bgp is not None
    originators = {o["device"] for o in bgp.inventory["originated"]}
    assert inject["host_name"] in originators
    assert inject.get("prefix")
    assert any(
        o["device"] == inject["host_name"] and o["prefix"] == inject["prefix"]
        for o in bgp.inventory["originated"]
    )
    assert inject.get("symptom_host")
    assert inject.get("probe_dst_ip")
    validate_benchmark_case(
        "isp_abilene",
        "bgp_missing_route_advertisement",
        inject,
        "",
        isp_options=isp_opts,
    )


def test_bgp_missing_route_advertisement_rejects_isp_non_originator() -> None:
    with pytest.raises(ValueError, match="not a BGP originator"):
        validate_benchmark_case(
            "isp_abilene",
            "bgp_missing_route_advertisement",
            {"host_name": "dnvrng"},
            "",
            isp_options={
                "topo": "abilene",
                "igp": "ospf",
                "bgp_mode": "ebgp",
                "rpki": False,
            },
        )


@pytest.mark.parametrize("topo_size", ["s", "m", "l"])
def test_bgp_missing_route_advertisement_enterprise_targets_edge(
    topo_size: str,
) -> None:
    inject = resolve_inject_params(
        "bgp_missing_route_advertisement",
        "enterprise_branch",
        topo_size,
        seed=42,
    )
    assert inject["host_name"].endswith("_edge")
    assert "_core" not in inject["host_name"]
    validate_benchmark_case(
        "enterprise_branch",
        "bgp_missing_route_advertisement",
        inject,
        topo_size,
    )


@pytest.mark.parametrize("topo_size", ["s", "m", "l"])
@pytest.mark.parametrize(
    "problem",
    ["host_static_blackhole", "bgp_hijacking"],
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


def test_mtu_mismatch_k8s_lab_uses_controller_facing_leaf() -> None:
    params = resolve_inject_params("mtu_mismatch", "k8s_lab", "s", seed=1)
    assert params["host_name"] == "leaf_1_1"
    assert params["intf_name"] == "eth2"


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
    """Spot-check that specialty failures remain resolvable on their home scenarios."""
    assert (
        resolve_inject_params(
            "wireguard_peer_key_misconfiguration", "enterprise_branch", "s"
        )
        is not None
    )
    assert (
        resolve_inject_params(
            "wireguard_allowed_ips_misconfiguration", "enterprise_branch", "s"
        )
        is not None
    )
    assert resolve_inject_params("vrf_dscp_remarking", "enterprise_branch", "s")
    assert resolve_inject_params(
        "p4_action_selector_member_misconfig", "p4_dc_fabric", ""
    )
    assert resolve_inject_params("p4_table_resource_exhaustion", "p4_dc_fabric", "")
    assert resolve_inject_params(
        "bgp_rpki_invalid_route_leak", "isp_abilene_ebgp_rpki", ""
    )


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


def test_p4_dc_fabric_corruption_pins_leaf_spine_on_probe_path() -> None:
    inject = resolve_inject_params(
        "link_packet_corruption", "p4_dc_fabric", "s", seed=0
    )
    assert inject["host_name"].startswith("leaf_")
    assert inject.get("probe_dst_ip")
    assert inject.get("observer_device")
    assert inject["corruption_percentage"] == "12"


def test_p4_dc_fabric_table_entry_failures_pin_probe_path() -> None:
    for problem in ("p4_table_entry_missing", "p4_table_entry_misconfig"):
        inject = resolve_inject_params(problem, "p4_dc_fabric", "s", seed=0)
        assert inject["host_name"] == "leaf_1", problem
        assert inject.get("probe_dst_ip") == "10.0.2.11", problem
        assert inject.get("observer_device") == "client_1_1", problem
        validate_benchmark_case("p4_dc_fabric", problem, inject, "s")
        for topo_size in ("s", "m", "l"):
            rows = enumerate_inject_params(problem, "p4_dc_fabric", topo_size)
            assert rows == [
                resolve_inject_params(problem, "p4_dc_fabric", topo_size, seed=42)
            ], topo_size


def test_p4_dc_fabric_pipeline_mismatch_pins_probe_path() -> None:
    for topo_size in ("s", "m", "l"):
        inject = resolve_inject_params(
            "p4runtime_pipeline_mismatch", "p4_dc_fabric", topo_size, seed=0
        )
        assert inject["host_name"] == "leaf_1", topo_size
        assert "observer_device" not in inject
        assert "probe_dst_ip" not in inject
        validate_benchmark_case(
            "p4_dc_fabric", "p4runtime_pipeline_mismatch", inject, topo_size
        )


def test_p4_dc_fabric_partial_write_pins_ingress_leaf() -> None:
    for topo_size in ("s", "m", "l"):
        for seed in range(5):
            inject = resolve_inject_params(
                "p4runtime_partial_write", "p4_dc_fabric", topo_size, seed=seed
            )
            assert inject["host_name"] == "leaf_1", (topo_size, seed)
        validate_benchmark_case(
            "p4_dc_fabric",
            "p4runtime_partial_write",
            resolve_inject_params(
                "p4runtime_partial_write", "p4_dc_fabric", topo_size, seed=0
            ),
            topo_size,
        )


def test_p4_dc_gateway_partial_write_pins_backend_leaf() -> None:
    from nika.net_env.p4_dc_gateway.topology_model import build_gateway_fabric_model

    for topo_size in ("s", "m", "l"):
        model = build_gateway_fabric_model(topo_size)  # type: ignore[arg-type]
        expected = model.backend_pool[0].attached_switch
        for seed in range(5):
            inject = resolve_inject_params(
                "p4runtime_partial_write", "p4_dc_gateway", topo_size, seed=seed
            )
            assert inject["host_name"] == expected, (topo_size, seed)
        validate_benchmark_case(
            "p4_dc_gateway",
            "p4runtime_partial_write",
            resolve_inject_params(
                "p4runtime_partial_write", "p4_dc_gateway", topo_size, seed=0
            ),
            topo_size,
        )


def test_p4_dc_gateway_pipeline_mismatch_pins_gateway() -> None:
    for topo_size in ("s", "m", "l"):
        inject = resolve_inject_params(
            "p4runtime_pipeline_mismatch", "p4_dc_gateway", topo_size, seed=0
        )
        assert inject["host_name"] == "gateway_1", topo_size
        validate_benchmark_case(
            "p4_dc_gateway", "p4runtime_pipeline_mismatch", inject, topo_size
        )


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


@pytest.mark.parametrize("yaml_name", ["working/cases.yaml"])
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


# --- link_flap / link_detach / incast / bmv2 / tcp_rwnd inject param contracts ---

TOPO_SIZE = "s"
FABRIC_SCENARIOS = ("sdn_l3_clos", "p4_dc_fabric")
CORE_FLAP_SCENARIOS = (
    "simple_bgp",
    "dc_clos",
    "campus_lan",
    "enterprise_branch",
    "p4_dc_gateway",
    "min3clos",
)


@pytest.mark.parametrize("seed", (0, 1, 4, 7))
@pytest.mark.parametrize("scenario", FABRIC_SCENARIOS)
def test_link_flap_fabric_inject_aligns_probe_path(scenario: str, seed: int) -> None:
    params = resolve_inject_params("link_flap", scenario, TOPO_SIZE, seed=seed)
    assert params["down_time"] == "1"
    assert params["up_time"] == "1"
    assert params["host_name"].startswith("leaf_")
    assert params.get("probe_dst_ip")
    assert params.get("observer_device", "").startswith("client_")


@pytest.mark.parametrize("seed", (0, 1, 4, 7))
@pytest.mark.parametrize("scenario", CORE_FLAP_SCENARIOS)
def test_link_flap_core_inject_timing_and_probe_fields(
    scenario: str, seed: int
) -> None:
    topo = "" if scenario in {"simple_bgp", "min3clos"} else TOPO_SIZE
    params = resolve_inject_params("link_flap", scenario, topo, seed=seed)
    assert params["down_time"] == "1"
    assert params["up_time"] == "1"
    assert params.get("host_name")

    if scenario == "p4_dc_gateway":
        assert params.get("probe_dst_ip", "").startswith("10.")
        assert params["probe_dst_ip"] != "20.0.0.1"
    elif scenario == "min3clos":
        assert params["host_name"] == "leaf1"
        assert params["intf_name"] == "e1-1"
        assert params["probe_dst_ip"] == "10.0.0.27"
        assert params["observer_device"] == "client1"
    elif scenario == "dc_clos":
        assert params["host_name"] == "client_0"
        assert params["probe_dst_ip"] == "10.0.1.2"
    elif scenario == "enterprise_branch":
        assert params["probe_dst_ip"] == "10.0.20.2"
    elif scenario == "simple_bgp":
        assert params["host_name"] == "pc1"


def test_link_flap_isp_inject_uses_one_second_flap() -> None:
    isp_options = {
        "topo": "abilene",
        "igp": "ospf",
        "bgp_mode": "ebgp",
        "rpki": False,
    }
    params = resolve_inject_params(
        "link_flap", "isp_abilene", "", seed=1, isp_options=isp_options
    )
    assert params["down_time"] == "1"
    assert params["up_time"] == "1"
    assert params.get("symptom_host")
    assert params.get("probe_dst_ip")


@pytest.mark.parametrize(
    ("scenario", "expected_host"),
    [
        ("enterprise_branch", "br1_corp_pc"),
        ("sdn_l3_clos", "client_1_1"),
        ("p4_dc_fabric", "client_1_1"),
        ("dc_clos", None),
    ],
)
def test_link_detach_inject_params_pin_probe_path(
    scenario: str, expected_host: str | None
) -> None:
    params = resolve_inject_params("link_detach", scenario, TOPO_SIZE, seed=0)
    if expected_host is not None:
        assert params["host_name"] == expected_host
    assert params.get("intf_name")


@pytest.mark.parametrize("seed", (0, 1, 4, 7))
@pytest.mark.parametrize(
    "scenario",
    [
        "campus_lan",
        "dc_clos",
        "enterprise_branch",
        "p4_dc_fabric",
        "p4_dc_gateway",
        "sdn_l3_clos",
    ],
)
def test_incast_inject_params_align_probe_with_host(scenario: str, seed: int) -> None:
    from nika.problems.support.probe_paths import get_probe_path

    params = resolve_inject_params(
        "incast_traffic_network_limitation", scenario, TOPO_SIZE, seed=seed
    )
    assert params.get("host_name")
    probe_dst = params.get("probe_dst_ip")
    assert probe_dst, params
    assert probe_dst != "20.0.0.1", "gateway must probe backend IP, not VIP"

    path = get_probe_path(scenario, topo_size=TOPO_SIZE)
    assert path is not None

    if scenario in {"sdn_l3_clos", "p4_dc_fabric"}:
        assert params["host_name"].startswith("web_")
        assert probe_dst == path.dst_ip
        assert params.get("observer_device") == path.src_host
    elif scenario == "p4_dc_gateway":
        assert params["host_name"].startswith("service_")
        assert probe_dst.startswith("10.")
        assert params.get("observer_device") == path.src_host
    elif scenario == "dc_clos":
        assert params["host_name"] == "webserver0_pod0"
        assert probe_dst == "10.0.1.2"
    elif scenario == "enterprise_branch":
        assert params["host_name"] == "hq_srv"
        assert probe_dst == "10.0.20.2"
    elif scenario == "campus_lan":
        assert params["host_name"] == "web_server_0"
        assert probe_dst == "10.200.0.3"


def test_incast_compatible_columns_include_http_labs() -> None:
    from nika.problems.registry import compatible_columns

    cols = set(compatible_columns("incast_traffic_network_limitation"))
    assert {
        "campus_lan",
        "dc_clos",
        "enterprise_branch",
        "p4_dc_fabric",
        "p4_dc_gateway",
        "sdn_l3_clos",
    }.issubset(cols)


@pytest.mark.parametrize("seed", range(5))
def test_bmv2_switch_down_inject_params_prefer_path_critical_switch(seed: int) -> None:
    fabric = resolve_inject_params(
        "bmv2_switch_down", "p4_dc_fabric", TOPO_SIZE, seed=seed
    )
    assert fabric["host_name"] == "leaf_1"
    gateway = resolve_inject_params(
        "bmv2_switch_down", "p4_dc_gateway", TOPO_SIZE, seed=seed
    )
    assert gateway["host_name"] == "gateway_1"


def test_sender_resource_contention_inject_params_target_dc_clos_http_server() -> None:
    params = resolve_inject_params("sender_resource_contention", "dc_clos", "s", seed=1)
    assert params["host_name"] == "webserver0_pod0"
    assert params["client_host"] == "client_0"
    assert params["large_url"].endswith("/large.bin")
    assert params["small_url"].endswith("/small.bin")
    assert float(params["cpu_quota"]) == 0.05


def test_cpu_quota_to_nano_cpus() -> None:
    from nika.problems.support.cpu_quota_helpers import (
        cpu_quota_to_nano_cpus,
        nano_cpus_to_cfs,
    )

    assert cpu_quota_to_nano_cpus(0.25) == 250_000_000
    assert cpu_quota_to_nano_cpus(1.0) == 1_000_000_000
    assert nano_cpus_to_cfs(250_000_000) == (100_000, 25_000)
    assert nano_cpus_to_cfs(0) == (100_000, -1)
    with pytest.raises(ValueError):
        cpu_quota_to_nano_cpus(0.0)


def test_tcp_receive_window_limited_inject_params_target_http_client() -> None:
    params = resolve_inject_params(
        "tcp_receive_window_limited", "enterprise_branch", "m", seed=1
    )
    assert params["host_name"] == "br1_corp_pc"
    assert params["sender_host"] == "hq_srv"
    assert params["large_url"].endswith("/large.bin")


def test_load_balancer_overload_inject_params_target_campus_lan_vip() -> None:
    params = resolve_inject_params("load_balancer_overload", "campus_lan", "s", seed=1)
    assert params["host_name"] == "load_balancer"
    assert params["client_host"].startswith("pc_")
    assert params["vip_url"].endswith("/small")
    assert "web99.local" in params["vip_url"]
    assert "web0.local" in params["control_url"]
    assert "20.200.0.2" in params["backend_url"]
    assert float(params["cpu_quota"]) > 0
    assert int(params["concurrency"]) >= 1
    assert int(params["load_workers"]) >= 1
    load_hosts = [h for h in params["load_client_hosts"].split(",") if h]
    assert len(load_hosts) == 1
    assert params["client_host"] not in load_hosts


def test_dns_lookup_latency_pins_probe_resolver() -> None:
    dc = resolve_inject_params("dns_lookup_latency", "dc_clos", "l", seed=42)
    assert dc["host_name"] == "dns_pod0"
    assert enumerate_inject_params("dns_lookup_latency", "dc_clos", "l") == [dc]
    campus = resolve_inject_params("dns_lookup_latency", "campus_lan", "m", seed=42)
    assert campus["host_name"] == "dns_server"


@pytest.mark.parametrize("topo_size", ["s", "m", "l"])
def test_dns_record_error_pairs_dns_host_with_owned_zone(topo_size: str) -> None:
    """dc_clos dns_pod{N} only serves zone pod{N}; inject targets must match."""
    params = resolve_inject_params("dns_record_error", "dc_clos", topo_size, seed=42)
    assert params["host_name"].removeprefix("dns_") == params["target_domain"]
    validate_benchmark_case("dc_clos", "dns_record_error", params, topo_size)
    for row in enumerate_inject_params("dns_record_error", "dc_clos", topo_size):
        assert row["host_name"].removeprefix("dns_") == row["target_domain"]
    with pytest.raises(ValueError, match="does not serve zone"):
        validate_benchmark_case(
            "dc_clos",
            "dns_record_error",
            {
                "host_name": "dns_pod0",
                "target_website": "web0",
                "target_domain": "pod1",
            },
            "m",
        )
