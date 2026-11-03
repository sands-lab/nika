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


@pytest.mark.parametrize("profile", ["iosxr", "routeros"])
def test_ospf_route_reads_reject_timeouts(profile):
    from nika.net_env.native_routers import native_platform

    runtime = SimpleNamespace(exec=lambda *args, **kwargs: "[TIMEOUT] command expired")
    ops = native_platform(profile).fault_ops(runtime, "r1")
    with pytest.raises(RuntimeError, match="r1"):
        ops.ospf_routes()


@pytest.mark.parametrize(
    ("record", "port", "shared", "template_id"),
    [
        ("single", "ether2", False, "*B"),
        ("multiple", "ether3", True, "*C"),
        ("networks", "ether3", True, "*D"),
        ("networks", "ether3", False, "*D"),
    ],
)
@pytest.mark.parametrize("operation", ["network_type", "timers"])
def test_routeros_ospf_template_identity_and_isolation(
    record, port, shared, template_id, operation
):
    from pathlib import Path

    from nika.net_env.utils.routeros.fault_ops import RouterOsFaultOps

    # RouterOS 7.21.5 output, including find IDs and terse rows, recorded in a lab.
    terse = (
        Path(__file__).parent / "fixtures" / f"routeros_ospf_{record}.terse"
    ).read_text()
    writes = []

    def execute(node, command, **kwargs):
        if ":foreach templateId" in command:
            return terse
        if "/ip/address/print terse where" in command:
            return f"0 address=10.0.0.3/31 network=10.0.0.2 interface={port}\n"
        if "/ip/address/print terse" in command:
            other = "ether2" if shared else port
            return (
                f"0 address=10.0.0.1/31 network=10.0.0.0 interface={other}\n"
                f"1 address=10.0.0.3/31 network=10.0.0.2 interface={port}\n"
            )
        writes.append(command)
        return ""

    ops = RouterOsFaultOps(SimpleNamespace(exec=execute), "r1")
    assert ops.ospf_network_type(port) == "point-to-point"
    assert ops.ospf_interface_timers(port) == (10, 40)
    change = (
        (lambda: ops.set_ospf_network_type(port, "non-broadcast"))
        if operation == "network_type"
        else lambda: ops.set_ospf_interface_timers(port, 5, 20)
    )
    if shared:
        with pytest.raises(RuntimeError, match="covers other interfaces"):
            change()
        assert writes == []
    else:
        change()
        assert len(writes) == 1
        expected = (
            "type=nbma"
            if operation == "network_type"
            else "hello-interval=5s dead-interval=20s"
        )
        assert (
            f"/routing ospf interface-template set {template_id} {expected}"
            in writes[0]
        )


def test_routeros_full_neighbor_matches_exact_port():
    from nika.net_env.utils.routeros.fault_ops import RouterOsFaultOps

    rows = "0 state=Full interface=ether20%main address=10.0.0.9\n"
    ops = RouterOsFaultOps(SimpleNamespace(exec=lambda *a, **k: rows), "r1")
    assert not ops.ospf_interface_neighbor_full("ether2")
    assert ops.ospf_interface_neighbor_full("ether20")


def test_iosxr_empty_bgp_entry_is_withdrawn():
    from nika.net_env.utils.iosxr.fault_ops import IosxrFaultOps

    # XRd 26.2.1 retains this header after withdrawing the last path.
    output = (
        "BGP routing table entry for 198.51.110.0/24\n"
        "Paths: (0 available, no best path)\n"
        "  Not advertised to any peer\n"
    )
    ops = IosxrFaultOps(SimpleNamespace(exec=lambda *a, **k: output), "r1")
    assert not ops.has_bgp_route("198.51.110.0/24")


@pytest.mark.parametrize(
    "failure", ["ospf_neighbor_missing", "ospf_hello_interval_mismatch"]
)
def test_ospf_recovery_retries_transient_observation_errors(monkeypatch, failure):
    from unittest.mock import Mock

    from nika.problems.registry import get_problem_class
    from nika.problems.routing_control_plane import ospf
    from nika.problems.support.polling import wait_until
    from nika.problems.support.router_ops import OspfNeighbor

    monkeypatch.setattr(
        ospf,
        "wait_until",
        lambda predicate, timeout, **kwargs: wait_until(
            predicate, timeout, interval_s=0
        ),
    )
    cls = get_problem_class(failure)
    problem = cls.__new__(cls)
    problem.net_env = SimpleNamespace(backend="kathara")
    params = cls.Params(host_name="r1", intf_name="eth0")
    if failure == "ospf_neighbor_missing":
        neighbors = [OspfNeighbor("1.1.1.1", "10.0.0.2", "eth0", "Full")]
        ops = SimpleNamespace(
            restore_config=Mock(),
            ospf_neighbors=Mock(
                side_effect=[ValueError("daemon restarting"), neighbors]
            ),
        )
        monkeypatch.setattr(ospf, "native_fault_ops", lambda *_: ops)
        problem._saved_config = "saved OSPF config"
        problem._native_neighbors = {("1.1.1.1", "10.0.0.2", "eth0")}
        problem._original_routes = {"192.0.2.0/24"}
        problem._route_prefixes = lambda _: {"192.0.2.0/24"}
        reading = ops.ospf_neighbors
    else:
        problem._original_timers = (10, 40)
        problem._healthy_link = {
            "r1": {"full": True, "interface_up": True, "routes": ["192.0.2.0/24"]}
        }
        problem._set_timers = Mock()
        problem._timers = lambda *_: (10, 40)
        problem.link_health = Mock(
            side_effect=[ValueError("daemon restarting"), problem._healthy_link]
        )
        reading = problem.link_health
    assert problem.recover_fault(params)["verified"]
    assert reading.call_count == 2


@pytest.mark.parametrize("status", [0, 255])
def test_routeros_cli_separates_empty_tables_from_silent_ssh_failure(
    monkeypatch, status
):
    import subprocess

    from nika.net_env.utils.routeros import fault_ops

    monkeypatch.setattr(fault_ops, "CLI_COMMAND", f"exit {status}")

    def execute(node, command, **kwargs):
        return subprocess.run(
            ["sh", "-c", command], text=True, capture_output=True, check=True
        ).stdout

    ops = fault_ops.RouterOsFaultOps(SimpleNamespace(exec=execute), "r1")
    if status:
        with pytest.raises(RuntimeError, match="RouterOS command failed on r1"):
            ops.ospf_routes()
        with pytest.raises(RuntimeError, match="RouterOS command failed on r1"):
            ops.bgp_established_peers()
    else:
        assert ops.ospf_routes() == set()
        assert ops.bgp_established_peers() == set()
