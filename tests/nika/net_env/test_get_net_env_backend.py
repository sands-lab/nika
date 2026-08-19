"""get_net_env_instance must construct labs that omit backend from __init__."""

from __future__ import annotations

from nika.net_env.net_env_pool import get_net_env_instance, resolve_scenario_ref


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
