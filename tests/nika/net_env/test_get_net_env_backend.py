"""get_net_env_instance must construct labs that omit backend from __init__."""

from __future__ import annotations

import pytest

from nika.net_env.net_env_pool import (
    get_net_env_instance,
    list_all_net_envs,
    resolve_scenario_id,
)


def test_dc_clos_accepts_backend_kwarg() -> None:
    env = get_net_env_instance("dc_clos", backend="kathara", topo_size="s")
    assert env.backend == "kathara"
    assert not hasattr(env, "workload")
    assert env.lab is not None


def test_dc_clos_services_are_built_by_default() -> None:
    env = get_net_env_instance("dc_clos", backend="kathara", topo_size="s")
    assert "dns_pod0" in env.lab.machines


def test_only_canonical_scenario_ids_resolve() -> None:
    assert resolve_scenario_id("dc_clos") == "dc_clos"
    for legacy in ("dc_clos_service", "ospf_enterprise_dhcp", "sdn_star", "p4_counter"):
        with pytest.raises(ValueError):
            resolve_scenario_id(legacy)


def test_campus_lan_builds_dhcp_topology() -> None:
    env = get_net_env_instance("campus_lan", backend="kathara", topo_size="s")
    assert "dhcp_server" in env.lab.machines
    assert env.name == "campus_lan"


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


def test_p4_counter_alias_is_rejected() -> None:
    assert "p4_counter" not in list_all_net_envs()
    with pytest.raises(ValueError):
        get_net_env_instance("p4_counter", backend="kathara", topo_size="s")
