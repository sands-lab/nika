"""Vendor dispatch of failure operations (no Docker)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nika.net_env.net_env_pool import get_net_env_instance
from nika.problems.support.router_ops import RouterFaultOps, router_fault_ops
from nika.runtime.base import RuntimeCapabilityError

pytestmark = pytest.mark.unit


def test_dispatch_follows_node_device_profile() -> None:
    env = get_net_env_instance("campus_lan", topo_size="s", device_profile="iosxr")
    problem = SimpleNamespace(net_env=env, runtime=object())
    ops = router_fault_ops(problem, "router_core_1")
    assert ops is not None and ops.profile == "iosxr"
    assert ops.node == "router_core_1"
    # FRR nodes keep the failure's default implementation.
    assert router_fault_ops(problem, "router_dist_1_1") is None
    frr = SimpleNamespace(net_env=get_net_env_instance("dc_clos", topo_size="s"))
    assert router_fault_ops(frr, "leaf_router_0_0") is None


def test_min3clos_srlinux_keeps_default_failure_ops() -> None:
    """``nokia_srlinux`` on min3clos is for VM sizing, not fault-ops dispatch."""
    srl = get_net_env_instance("min3clos", device_profile="nokia_srlinux")
    sonic = get_net_env_instance("min3clos", device_profile="sonic")
    assert srl.machine_identities["leaf1"].capabilities == (
        "bgp",
        "linux",
        "nokia_srlinux",
        "srl",
    )
    assert sonic.machine_identities["leaf1"].capabilities == ("bgp", "linux", "sonic")
    problem_srl = SimpleNamespace(net_env=srl, runtime=object())
    problem_sonic = SimpleNamespace(net_env=sonic, runtime=object())
    assert router_fault_ops(problem_srl, "leaf1") is None
    ops = router_fault_ops(problem_sonic, "leaf1")
    assert ops is not None and ops.profile == "sonic"


def test_unimplemented_operation_is_a_capability_error() -> None:
    with pytest.raises(RuntimeCapabilityError, match="not supported on native"):
        RouterFaultOps(object(), "r1").set_bgp_asn(65000)
