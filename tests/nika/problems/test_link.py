"""Unit tests for controller-side link faults (no Docker)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nika.problems.link_interface.link import (
    LinkCapacityBottleneck,
    LinkCapacityBottleneckParams,
    LinkDetach,
    LinkDetachParams,
    LinkFailure,
    LinkFailureParams,
    LinkFlap,
    LinkFlapParams,
    parse_ip_route_json,
    static_route_replace_command,
)
from nika.runtime.base import RuntimeCapabilityError
import nika.problems.link_interface.link as link_mod
from nika.workflows.failure.inject import _refresh_injection_params

pytestmark = pytest.mark.unit


def _problem(cls, runtime, **attrs):
    problem = object.__new__(cls)
    problem.runtime = runtime
    problem.net_env = SimpleNamespace(backend=getattr(runtime, "backend", None))
    problem.faulty_intf = "eth0"
    problem._resolved_params = None
    problem._detach_holder = None
    problem._saved_addrs = []
    problem._saved_routes = []
    problem._controller_target = None
    problem._host_veth = None
    problem._capacity_mode = None
    problem._link_down_target = None
    for key, value in attrs.items():
        setattr(problem, key, value)
    return problem


class _Host:
    """HostTcController stand-in that records calls or raises if the peer is missing."""

    def __init__(
        self, runtime, *, peer: str = "nwabc123", missing: bool = False
    ) -> None:
        del runtime
        self.peer = peer
        self.missing = missing
        self.calls: list[tuple[str, str]] = []

    def _require(self) -> str:
        if self.missing:
            raise RuntimeCapabilityError("host veth is absent")
        return self.peer

    def peer_name(self, host: str, intf: str) -> str:
        del host, intf
        return self._require()

    def egress_dev(self, host: str, intf: str) -> str:
        return self.peer_name(host, intf)

    def start_link_flap(self, peer: str, down_time: int, up_time: int) -> None:
        del down_time, up_time
        self.calls.append(("start", peer))

    def link_flap_running(self, target: str) -> bool:
        self.calls.append(("running", target))
        return True

    def stop_link_flap(self, target: str) -> None:
        self.calls.append(("stop", target))

    def link_peer_down(self, peer: str) -> bool:
        raise AssertionError("no peer")

    def clear(self, peer: str) -> None:
        raise AssertionError("must not clear a missing peer")


def test_link_flap_records_and_uses_the_host_peer(monkeypatch) -> None:
    assert "_controller_target" in LinkFlap.RECORDED_ATTRS
    runtime = SimpleNamespace(backend="containerlab")
    host = _Host(runtime)
    monkeypatch.setattr(link_mod, "HostTcController", lambda rt: host)
    problem = _problem(LinkFlap, runtime)
    params = LinkFlapParams(host_name="leaf1", intf_name="eth1", down_time=1, up_time=1)
    problem.inject_fault(params)
    problem.faulty_intf = "eth1"
    assert problem._controller_target == "nwabc123"
    assert ("start", "nwabc123") in host.calls
    assert problem.verify_fault(params)["verified"] is True
    assert problem.recover_fault(params)["verified"] is False
    assert ("running", "nwabc123") in host.calls
    assert ("stop", "nwabc123") in host.calls

    row = {
        "problem_class": "LinkFlap",
        "_controller_target": None,
        "faulty_intf": "eth0",
        "resolved_params": {"host_name": "leaf1"},
    }
    _refresh_injection_params(problem, ["link_flap"], [(0, "link_flap", row)])
    assert row["_controller_target"] == "nwabc123"
    assert row["faulty_intf"] == "eth1"

    recorded = _Host(runtime, peer="nwrecorded")

    def boom(*args, **kwargs):
        raise AssertionError("recorded target should win")

    recorded.peer_name = boom
    monkeypatch.setattr(link_mod, "HostTcController", lambda rt: recorded)
    held = _problem(LinkFlap, runtime, _controller_target="nwrecorded")
    assert held.verify_fault(LinkFlapParams(host_name="leaf1", intf_name="eth1"))[
        "verified"
    ]
    assert ("running", "nwrecorded") in recorded.calls


@pytest.mark.parametrize(
    ("cls", "params", "method"),
    [
        (
            LinkFlap,
            LinkFlapParams(host_name="leaf1", intf_name="eth1", down_time=1, up_time=1),
            "inject_fault",
        ),
        (
            LinkFailure,
            LinkFailureParams(host_name="leaf1", intf_name="eth1"),
            "verify_fault",
        ),
        (
            LinkCapacityBottleneck,
            LinkCapacityBottleneckParams(host_name="leaf1", intf_name="eth1"),
            "recover_fault",
        ),
    ],
)
def test_host_link_faults_raise_when_the_host_peer_is_missing(
    monkeypatch, cls, params, method
) -> None:
    runtime = SimpleNamespace(
        backend="containerlab",
        get_interface_operstate=lambda host, intf: "down",
        tc_clear_intf=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not mutate the node")
        ),
    )
    monkeypatch.setattr(
        link_mod, "HostTcController", lambda rt: _Host(rt, missing=True)
    )
    problem = _problem(cls, runtime)
    with pytest.raises(RuntimeCapabilityError, match="absent"):
        getattr(problem, method)(params)


def _detach_runtime(*, intf_present: bool):
    holder = MagicMock()
    holder.id = "holder1"
    node = MagicMock()
    node.id = "node1"
    node.client.containers.run.return_value = holder
    runtime = SimpleNamespace(
        backend="containerlab",
        lab_name="lab",
        get_container=lambda host: node,
        interface_exists=lambda host, intf: intf_present,
        exec=lambda *a, **k: "[]",
    )
    return runtime, holder


@pytest.mark.parametrize("succeeds", [False, True])
def test_link_detach_holder_follows_inject_outcome(monkeypatch, succeeds: bool) -> None:
    runtime, holder = _detach_runtime(intf_present=not succeeds)
    problem = _problem(LinkDetach, runtime)
    monkeypatch.setattr(link_mod, "native_fault_ops", lambda *a, **k: None)
    monkeypatch.setattr(link_mod, "nika_image", lambda name: "nika/base:x")
    monkeypatch.setattr(problem, "_move_netns", lambda *a, **k: None)
    if succeeds:
        problem.inject_fault(LinkDetachParams(host_name="leaf1", intf_name="eth1"))
        holder.remove.assert_not_called()
        assert problem._detach_holder == "holder1"
        return
    calls = {"n": 0}

    def fake_time() -> float:
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 6.0

    monkeypatch.setattr(link_mod.time, "time", fake_time)
    monkeypatch.setattr(link_mod.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="still present"):
        problem.inject_fault(LinkDetachParams(host_name="leaf1", intf_name="eth1"))
    holder.remove.assert_called_with(force=True)
    assert problem._detach_holder is None


def test_link_detach_verify_uses_nos_port_when_the_provider_follows_link(
    monkeypatch,
) -> None:
    class Ops:
        def __init__(self, up: bool, follows: bool = True) -> None:
            self.up = up
            self.port_follows_link = follows

        def interface_up(self, intf: str) -> bool:
            return self.up

    runtime = SimpleNamespace(
        backend="containerlab", interface_exists=lambda host, intf: False
    )
    params = LinkDetachParams(host_name="leaf1", intf_name="eth1")
    problem = _problem(LinkDetach, runtime)

    monkeypatch.setattr(link_mod, "native_fault_ops", lambda *a, **k: Ops(False))
    down = problem.verify_fault(params)
    assert down["verified"] is True and down["details"]["router_port_up"] is False

    monkeypatch.setattr(link_mod, "native_fault_ops", lambda *a, **k: Ops(True))
    assert problem.verify_fault(params)["verified"] is False

    monkeypatch.setattr(
        link_mod, "native_fault_ops", lambda *a, **k: Ops(True, follows=False)
    )
    xrd = problem.verify_fault(params)
    assert xrd["verified"] is True and "router_port_up" not in xrd["details"]

    monkeypatch.setattr(link_mod, "native_fault_ops", lambda *a, **k: None)
    linux = problem.verify_fault(
        LinkDetachParams(host_name="client1", intf_name="eth1")
    )
    assert linux["verified"] is True and "router_port_up" not in linux["details"]


def test_static_route_replace_skips_dynamic_and_keeps_onlink_nexthops() -> None:
    skipped = [
        ("-6", {"dst": "default", "gateway": "fe80::1", "protocol": "ra"}),
        ("-6", {"dst": "1000:10::/64", "protocol": "kernel"}),
        (
            "-4",
            {
                "dst": "10.0.0.0/8",
                "protocol": "boot",
                "nexthops": [{"gateway": "10.0.0.1", "dev": "eth2"}],
            },
        ),
        ("-4", {"dst": "default", "table": 255}),
    ]
    for family, route in skipped:
        assert static_route_replace_command(family, route, "eth1") is None
    default = static_route_replace_command(
        "-6",
        {
            "dst": "default",
            "gateway": "1000:10::24",
            "protocol": "boot",
            "metric": 1024,
        },
        "eth1",
    )
    assert (
        default is not None and "via 1000:10::24" in default and "proto boot" in default
    )
    link_local = static_route_replace_command(
        "-6", {"dst": "default", "gateway": "fe80::1", "protocol": "static"}, "eth1"
    )
    assert link_local is not None and link_local.endswith("onlink")
    tabled = static_route_replace_command(
        "-4",
        {
            "dst": "10.9.0.0/16",
            "gateway": "10.1.1.1",
            "protocol": "boot",
            "table": 100,
            "src": "10.1.1.2",
        },
        "eth1",
    )
    assert tabled is not None and "table 100" in tabled and "src 10.1.1.2" in tabled
    multipath = static_route_replace_command(
        "-4",
        {
            "dst": "10.0.0.0/8",
            "protocol": "boot",
            "nexthops": [
                {"gateway": "10.0.0.1", "dev": "eth1", "weight": 1},
                {"gateway": "10.0.0.2", "dev": "eth1", "weight": 1},
            ],
        },
        "eth1",
    )
    assert multipath is not None and multipath.count("nexthop") == 2


def test_parse_ip_route_json_and_restore_surfaces_replace_failure() -> None:
    assert parse_ip_route_json("") == []
    assert parse_ip_route_json("[]") == []
    assert parse_ip_route_json("not-json") is None
    runtime = SimpleNamespace(
        backend="containerlab",
        exec=lambda host, cmd: "RTNETLINK: File exists",
    )
    problem = _problem(LinkDetach, runtime)
    problem._saved_routes = [
        "ip -6 route replace default via 1000:10::24 proto boot dev eth1"
    ]
    with pytest.raises(RuntimeError, match="restoring route failed"):
        problem._restore_l3_state("client1", "eth1")
    runtime.exec = lambda host, cmd: "NIKA_ROUTE_OK"
    problem._restore_l3_state("client1", "eth1")
    assert problem._saved_routes == []
