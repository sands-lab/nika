"""IOS XR device profiles for Ethernet scenarios (no Docker)."""

from __future__ import annotations

import pytest

from nika.net_env.net_env_pool import get_net_env_instance, scenario_device_profiles
from nika.net_env.utils.iosxr.frr import translate_frr_router
from nika.workflows.env.start import _resolve_isp_kwargs

pytestmark = pytest.mark.unit


def _profile_kwargs(scenario: str, profile: str | None) -> dict:
    return _resolve_isp_kwargs(
        scenario,
        topo=None,
        igp=None,
        metric_strategy=None,
        constant_metric=None,
        bgp_mode=None,
        device_profile=profile,
    )


def test_scenarios_declare_native_profiles() -> None:
    assert scenario_device_profiles("dc_clos") == ("iosxr",)
    assert _profile_kwargs("dc_clos", "iosxr") == {"device_profile": "iosxr"}
    # frr selects the unchanged scenario default.
    assert _profile_kwargs("dc_clos", "frr") == {}
    assert _profile_kwargs("dc_clos", None) == {}
    for scenario in ("campus_lan", "enterprise_branch"):
        with pytest.raises(ValueError, match="does not support device_profile"):
            _profile_kwargs(scenario, "iosxr")


def test_dc_clos_keeps_its_design_on_iosxr() -> None:
    frr = get_net_env_instance("dc_clos", topo_size="s")
    xr = get_net_env_instance("dc_clos", topo_size="s", device_profile="iosxr")
    assert type(frr).__name__ == "DCClos"
    assert set(xr.lab.machines) == set(frr.lab.machines)
    assert xr.bgp_neighbors == frr.bgp_neighbors
    assert xr.routers == frr.routers
    for name in xr.routers:
        identity = xr.machine_identities[name]
        assert "iosxr" in identity.capabilities and "frr" not in identity.capabilities
        config = xr.deployment_configs[name]
        assert config.count(" neighbor ") == len(frr.bgp_neighbors[name])
        # FRR accepts all eBGP routes without ebgp-requires-policy; XR needs
        # an explicit pass policy for the same behavior.
        assert config.count("route-policy PASS in") == len(frr.bgp_neighbors[name])
        assert "maximum-paths ebgp 32" in config
    leaf = xr.deployment_configs["leaf_router_0_0"]
    assert "network 10.0.0.0/24" in leaf
    with pytest.raises(ValueError, match="supports size s only"):
        get_net_env_instance("dc_clos", topo_size="m", device_profile="iosxr")


def test_translation_rejects_unmodeled_router_behavior() -> None:
    startup = "ip addr add 10.0.0.1/24 dev eth0\nservice frr start\n"
    with pytest.raises(ValueError, match="Unsupported FRR statement"):
        translate_frr_router(
            "r1", "router ospf\n network 10.0.0.0/24 area 0\n", startup
        )
    with pytest.raises(ValueError, match="Unsupported IOS XR router startup"):
        translate_frr_router("r1", "router bgp 1\n", startup + "brctl addbr br0\n")
