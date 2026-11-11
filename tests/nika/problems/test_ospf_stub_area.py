"""Area-type observations used to distinguish rejected OSPF Hellos."""

import pytest

from nika.problems.routing_control_plane.ospf import _ospf_area_modes

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "profile,output,expected",
    [
        (
            None,
            "router ospf\n network 172.16.0.6/31 area 1\n area 1 stub\n!\n",
            {"0.0.0.1": "stub"},
        ),
        (
            None,
            "interface eth0\n ip ospf area 0.0.0.1\n!\nrouter ospf\n!\n",
            {"0.0.0.1": "normal"},
        ),
        (
            "iosxr",
            "router ospf CORE\n area 0\n !\n area 1\n  stub\n  interface GigabitEthernet0/0/0/0\n  !\n !\n area 2\n  nssa\n !\n!\n",
            {"0.0.0.0": "normal", "0.0.0.1": "stub", "0.0.0.2": "nssa"},
        ),
        (
            "routeros",
            "0 name=backbone area-id=0.0.0.0 type=default\n1 name=area1 area-id=0.0.0.1 type=stub\n",
            {"0.0.0.0": "normal", "0.0.0.1": "stub"},
        ),
    ],
)
def test_area_type_observations(profile, output, expected):
    assert _ospf_area_modes(output, profile) == expected


@pytest.mark.parametrize("profile", [None, "iosxr", "routeros"])
def test_failed_observation_does_not_establish_a_normal_area(profile):
    assert _ospf_area_modes("[TIMEOUT]", profile) == {}


@pytest.mark.parametrize("profile", ["frr", "iosxr", "routeros"])
def test_benchmark_targets_the_native_platform_when_available(profile):
    import random
    from types import SimpleNamespace

    from nika.net_env.campus_lan.lab import CampusLan
    from nika.problems.routing_control_plane.ospf import OspfStubAreaMismatch
    from nika.problems.support.router_ops import native_router_profile

    env = CampusLan(topo_size="s", device_profile=profile)
    ctx = SimpleNamespace(net_env=env, rng=random.Random(1))
    inject = OspfStubAreaMismatch.benchmark_inject_params(ctx)
    assert native_router_profile(env, inject["host_name"]) == (
        None if profile == "frr" else profile
    )
    OspfStubAreaMismatch.validate_benchmark_inject(ctx, inject)
