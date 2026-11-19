"""Scenario registry contract: canonical IDs and ISP profile wiring."""

from __future__ import annotations

import pytest

from nika.net_env.net_env_pool import (
    get_net_env_instance,
    resolve_scenario_id,
)

pytestmark = pytest.mark.unit


def test_only_canonical_scenario_ids_resolve() -> None:
    assert resolve_scenario_id("dc_clos") == "dc_clos"
    for legacy in ("dc_clos_service", "ospf_enterprise_dhcp"):
        with pytest.raises(ValueError):
            resolve_scenario_id(legacy)


def test_base_ebgp_deployment_carries_rpki() -> None:
    env = get_net_env_instance(
        "isp_abilene", backend="kathara", igp="ospf", bgp_mode="ebgp"
    )
    assert env.name == "isp_abilene"
    assert env.rpki is True
    assert str(env.topo) == "abilene"
    assert env.igp == "ospf"
    assert env.bgp_plan is not None
    assert env.bgp_plan.mode == "ebgp"
    assert env.bgp_plan.inventory.get("rpki") is True
    assert "routinator" in env.lab.machines
    from nika.mcp.registry import select_diagnosis_servers

    assert "kathara_frr_mcp_server" in select_diagnosis_servers(
        "isp_abilene", backend="kathara", deployment={"bgp_mode": "ebgp"}
    )


def test_isp_defaults_to_kathara_without_explicit_backend() -> None:
    env = get_net_env_instance("isp_abilene", igp="ospf", bgp_mode="ebgp")
    assert env.backend == "kathara"
