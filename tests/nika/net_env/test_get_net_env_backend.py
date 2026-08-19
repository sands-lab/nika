"""get_net_env_instance must construct labs that omit backend from __init__."""

from __future__ import annotations

from nika.net_env.net_env_pool import (
    get_net_env_instance,
    list_all_net_envs,
    resolve_scenario_ref,
)


def test_dc_clos_alias_service_accepts_backend_kwarg() -> None:
    env = get_net_env_instance("dc_clos_service", backend="kathara", topo_size="s")
    assert env.backend == "kathara"
    assert env.workload == "service"
    assert env.lab is not None


def test_dc_clos_host_default() -> None:
    env = get_net_env_instance("dc_clos", backend="kathara", topo_size="s")
    assert env.workload == "host"
    assert "pc_0_0" in env.lab.machines


def test_resolve_scenario_ref_aliases() -> None:
    assert resolve_scenario_ref("dc_clos_bgp") == ("dc_clos", "host")
    assert resolve_scenario_ref("dc_clos_service") == ("dc_clos", "service")
    assert resolve_scenario_ref("dc_clos") == ("dc_clos", None)
    assert resolve_scenario_ref("ospf_enterprise_static") == ("campus_lan", "static")
    assert resolve_scenario_ref("ospf_enterprise_dhcp") == ("campus_lan", "dhcp")
    assert resolve_scenario_ref("campus_lan") == ("campus_lan", None)
    assert resolve_scenario_ref("sdn_star") == ("sdn_l3_clos", None)
    assert resolve_scenario_ref("sdn_clos") == ("sdn_l3_clos", None)
    assert resolve_scenario_ref("p4_counter") == ("p4_dc_fabric", None)


def test_campus_lan_alias_default_workload() -> None:
    env = get_net_env_instance("ospf_enterprise_dhcp", backend="kathara", topo_size="s")
    assert env.workload == "dhcp"
    assert "dhcp_server" in env.lab.machines

    env_s = get_net_env_instance(
        "ospf_enterprise_static", backend="kathara", topo_size="s"
    )
    assert env_s.workload == "static"
    assert any(name.startswith("switch_dist_") for name in env_s.lab.machines)

    env_c = get_net_env_instance("campus_lan", backend="kathara", topo_size="s")
    assert env_c.workload == "static"
    assert env_c.name == "campus_lan"


def test_simple_bgp_forwards_backend() -> None:
    env = get_net_env_instance("simple_bgp", backend="kathara")
    assert env.backend == "kathara"
    assert env.lab is not None


def test_isp_rpki_abilene_profile() -> None:
    env = get_net_env_instance(
        "isp",
        backend="kathara",
        topo="abilene",
        igp="ospf",
        bgp_mode="ebgp",
        rpki=True,
    )
    assert env.backend == "kathara"
    assert env.LAB_NAME == "isp"
    assert env.bgp_mode == "ebgp"
    assert env.rpki is True
    assert str(env.topo) == "abilene"
    assert env.igp == "ospf"
    assert env.bgp_plan is not None
    assert env.bgp_plan.mode == "ebgp"
    assert env.bgp_plan.inventory.get("rpki") is True
    assert "routinator" in env.lab.machines
    from nika.service.mcp_server.registry import select_diagnosis_servers

    assert "kathara_frr_mcp_server" in select_diagnosis_servers(
        "isp", backend="kathara"
    )


def test_p4_counter_alias_resolves_to_fabric() -> None:
    assert "p4_counter" not in list_all_net_envs()
    env = get_net_env_instance("p4_counter", backend="kathara", topo_size="s")
    assert env.backend == "kathara"
    assert "leaf_1" in env.lab.machines


def test_p4_counter_benchmark_row_rewrites_to_fabric() -> None:
    from nika.workflows.benchmark.load_config import normalize_benchmark_row

    row = normalize_benchmark_row(
        {
            "scenario": "p4_counter",
            "problem": "bmv2_switch_down",
            "topo_size": None,
            "inject": {"host_name": "s1"},
        }
    )
    assert row["scenario"] == "p4_dc_fabric"
    assert row["topo_size"] == "s"
    assert row["inject"]["host_name"] == "s1"
