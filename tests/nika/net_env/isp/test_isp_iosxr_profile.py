"""Unit tests for ISP device_profile pairing (no Docker)."""

from __future__ import annotations

import pytest

from nika.net_env.isp.profiles import (
    normalize_device_profile,
    validate_backend_profile,
)
from nika.workflows.env.start import _resolve_isp_kwargs

pytestmark = pytest.mark.unit


def test_iosxr_profile_normalizes() -> None:
    assert normalize_device_profile("iosxr") == "iosxr"
    assert normalize_device_profile("IOSXR") == "iosxr"


def test_kathara_iosxr_pair_allowed() -> None:
    validate_backend_profile("kathara", "iosxr")


def test_containerlab_iosxr_pair_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported isp pairing"):
        validate_backend_profile("containerlab", "iosxr")


@pytest.mark.parametrize("profile", ["iosxr", "routeros"])
def test_native_isp_profiles_bind_their_igp(profile) -> None:
    kwargs = _resolve_isp_kwargs(
        "isp_abilene",
        topo=None,
        igp=None,
        metric_strategy=None,
        constant_metric=None,
        bgp_mode=None,
        rpki=None,
        device_profile=profile,
        backend="kathara",
    )
    assert kwargs["device_profile"] == profile
    from nika.net_env.net_env_pool import get_net_env_instance

    env = get_net_env_instance("isp_abilene", **kwargs)
    assert env.plan.igp == "isis"
    isis = "router isis CORE" if profile == "iosxr" else "/routing isis instance"
    assert all(isis in c for c in env.deployment_configs.values())

    kwargs["igp"] = "ospf"
    env = get_net_env_instance("isp_abilene", **kwargs)
    assert env.device_profile == profile
    assert env.plan.igp == "ospf"
    assert env.ROUTING_MODELS == {f"{profile}-isp"}


@pytest.mark.parametrize(("igp", "bgp_mode"), [("ospf", "ebgp"), ("isis", "ibgp_rr")])
def test_iosxr_interfaces_precede_routing_modes(igp, bgp_mode) -> None:
    # XR resolves an ``interface`` line in the innermost mode that accepts it.
    # A top-level interface after an IGP block would become an IGP interface.
    from nika.net_env.net_env_pool import get_net_env_instance

    env = get_net_env_instance(
        "isp_abilene", device_profile="iosxr", igp=igp, bgp_mode=bgp_mode
    )
    originating = 0
    for config in env.deployment_configs.values():
        lines = config.splitlines()
        first_router = next(
            i for i, line in enumerate(lines) if line.startswith("router ")
        )
        top_interfaces = [
            i for i, line in enumerate(lines) if line.startswith("interface ")
        ]
        assert max(top_interfaces) < first_router
        originating += "interface Loopback1" in lines
    assert originating


def test_native_isp_omits_the_kathara_customer_site_and_runs_stub_workload() -> None:
    from nika.net_env.net_env_pool import get_net_env_instance

    env = get_net_env_instance(
        "isp_abilene", device_profile="iosxr", igp="ospf", bgp_mode="ebgp"
    )
    assert env.bgp_plan is not None
    assert env.bgp_plan.customer_site is None
    assert "customer_site" not in env.bgp_plan.inventory
    stubs = {host.host_name for host in env.traffic.hosts}
    workload = env.business_workload()
    assert workload.processes
    assert set(workload.nodes) <= stubs
    assert env.probe_intent() is not None


def test_iosxr_isis_adjacency_table_parses() -> None:
    from nika.net_env.isp.iosxr import parse_iosxr_isis_adjacencies

    output = """
IS-IS CORE Level-2 adjacencies:
System Id      Interface                SNPA           State Hold Changed  NSF IPv4 IPv6
                                                                               BFD  BFD
ucla           Gi0/0/0/1                *PtoP*         Up    26   00:00:32 Yes None None
utah           Gi0/0/0/2                *PtoP*         Init  25   00:00:35 Yes None None

Total adjacency count: 2
"""
    assert parse_iosxr_isis_adjacencies(output) == {("ucla", "GigabitEthernet0/0/0/1")}
    with pytest.raises(ValueError, match="no recognizable IS-IS"):
        parse_iosxr_isis_adjacencies("% Invalid input detected at '^' marker.")
