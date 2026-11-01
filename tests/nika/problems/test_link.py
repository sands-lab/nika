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


def test_link_flap_records_controller_target() -> None:
    assert "_controller_target" in LinkFlap.RECORDED_ATTRS


def test_link_flap_verify_and_recover_discover_host_peer(monkeypatch) -> None:
    seen: dict[str, str] = {}

    class Controller:
        def __init__(self, runtime) -> None:
            pass

        def peer_name(self, host: str, intf: str) -> str:
            seen["peer"] = f"{host}:{intf}"
            return "nwabc123"

        def link_flap_running(self, target: str) -> bool:
            seen["running"] = target
            return True

        def stop_link_flap(self, target: str) -> None:
            seen["stop"] = target

        def start_node_link_flap(self, *args, **kwargs):
            raise AssertionError("must not flap inside the node")

        def stop_node_link_flap(self, *args, **kwargs):
            raise AssertionError("must not stop a node worker")

    runtime = SimpleNamespace(backend="containerlab")
    problem = _problem(LinkFlap, runtime)
    params = LinkFlapParams(host_name="leaf1", intf_name="eth1")
    monkeypatch.setattr(link_mod, "HostTcController", Controller)
    result = problem.verify_fault(params)
    recovered = problem.recover_fault(params)
    assert result["verified"] is True
    assert recovered["verified"] is False
    assert seen["running"] == "nwabc123"
    assert seen["stop"] == "nwabc123"


def test_link_flap_uses_recorded_controller_target(monkeypatch) -> None:
    seen: dict[str, str] = {}

    class Controller:
        def __init__(self, runtime) -> None:
            pass

        def peer_name(self, host: str, intf: str) -> str:
            raise AssertionError("recorded target should win")

        def link_flap_running(self, target: str) -> bool:
            seen["running"] = target
            return True

    runtime = SimpleNamespace(backend="containerlab")
    problem = _problem(LinkFlap, runtime, _controller_target="nwrecorded")
    monkeypatch.setattr(link_mod, "HostTcController", Controller)
    result = problem.verify_fault(LinkFlapParams(host_name="leaf1", intf_name="eth1"))
    assert result["verified"] is True
    assert seen["running"] == "nwrecorded"


def test_link_flap_inject_raises_when_host_peer_is_missing(monkeypatch) -> None:
    class Controller:
        def __init__(self, runtime) -> None:
            pass

        def peer_name(self, host: str, intf: str) -> str:
            raise RuntimeCapabilityError("host veth is absent")

        def start_node_link_flap(self, *args, **kwargs):
            raise AssertionError("must not flap inside the node")

        def start_link_flap(self, *args, **kwargs):
            raise AssertionError("must not start a host worker")

    runtime = SimpleNamespace(backend="containerlab")
    problem = _problem(LinkFlap, runtime)
    monkeypatch.setattr(link_mod, "HostTcController", Controller)
    with pytest.raises(RuntimeCapabilityError, match="absent"):
        problem.inject_fault(
            LinkFlapParams(host_name="leaf1", intf_name="eth1", down_time=1, up_time=1)
        )


def test_link_down_verify_raises_when_host_peer_is_missing(monkeypatch) -> None:
    class Controller:
        def __init__(self, runtime) -> None:
            pass

        def peer_name(self, host: str, intf: str) -> str:
            raise RuntimeCapabilityError("host veth is absent")

        def set_node_link_down(self, *args, **kwargs):
            raise AssertionError("must not mutate the node")

        def link_peer_down(self, peer: str) -> bool:
            raise AssertionError("no peer")

    runtime = SimpleNamespace(
        backend="containerlab",
        get_interface_operstate=lambda host, intf: "down",
    )
    problem = _problem(LinkFailure, runtime)
    monkeypatch.setattr(link_mod, "HostTcController", Controller)
    with pytest.raises(RuntimeCapabilityError, match="absent"):
        problem.verify_fault(LinkFailureParams(host_name="leaf1", intf_name="eth1"))


def test_capacity_recover_raises_when_host_peer_is_missing(monkeypatch) -> None:
    class Controller:
        def __init__(self, runtime) -> None:
            pass

        def egress_dev(self, host: str, intf: str) -> str:
            raise RuntimeCapabilityError("host veth is absent")

        def clear(self, peer: str) -> None:
            raise AssertionError("must not clear a missing peer")

    runtime = SimpleNamespace(
        backend="containerlab",
        tc_clear_intf=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not mutate the node")
        ),
    )
    problem = _problem(LinkCapacityBottleneck, runtime)
    monkeypatch.setattr(link_mod, "HostTcController", Controller)
    with pytest.raises(RuntimeCapabilityError, match="absent"):
        problem.recover_fault(
            LinkCapacityBottleneckParams(host_name="leaf1", intf_name="eth1")
        )


def test_link_detach_timeout_removes_holder(monkeypatch) -> None:
    holder = MagicMock()
    holder.id = "holder1"
    node = MagicMock()
    node.id = "node1"
    node.client.containers.run.return_value = holder
    runtime = SimpleNamespace(
        backend="containerlab",
        lab_name="lab",
        get_container=lambda host: node,
        interface_exists=lambda host, intf: True,
        exec=lambda *a, **k: "[]",
    )
    problem = _problem(LinkDetach, runtime)
    monkeypatch.setattr(link_mod, "native_fault_ops", lambda *a, **k: None)
    monkeypatch.setattr(link_mod, "nika_image", lambda name: "nika/base:x")
    monkeypatch.setattr(problem, "_move_netns", lambda *a, **k: None)
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


def test_link_detach_keeps_holder_when_inject_succeeds(monkeypatch) -> None:
    holder = MagicMock()
    holder.id = "holder1"
    node = MagicMock()
    node.id = "node1"
    node.client.containers.run.return_value = holder
    runtime = SimpleNamespace(
        backend="containerlab",
        lab_name="lab",
        get_container=lambda host: node,
        interface_exists=lambda host, intf: False,
        exec=lambda *a, **k: "[]",
    )
    problem = _problem(LinkDetach, runtime)
    monkeypatch.setattr(link_mod, "native_fault_ops", lambda *a, **k: None)
    monkeypatch.setattr(link_mod, "nika_image", lambda name: "nika/base:x")
    monkeypatch.setattr(problem, "_move_netns", lambda *a, **k: None)
    problem.inject_fault(LinkDetachParams(host_name="leaf1", intf_name="eth1"))
    holder.remove.assert_not_called()
    assert problem._detach_holder == "holder1"


def test_link_detach_verify_requires_nos_port_down(monkeypatch) -> None:
    class Ops:
        port_follows_link = True

        def __init__(self, up: bool) -> None:
            self.up = up

        def interface_up(self, intf: str) -> bool:
            seen.append(intf)
            return self.up

    seen: list[str] = []
    runtime = SimpleNamespace(
        backend="containerlab",
        interface_exists=lambda host, intf: False,
    )
    problem = _problem(LinkDetach, runtime)
    monkeypatch.setattr(link_mod, "native_fault_ops", lambda *a, **k: Ops(False))
    result = problem.verify_fault(LinkDetachParams(host_name="leaf1", intf_name="eth1"))
    assert result["verified"] is True
    assert result["details"]["intf"] == "eth1"
    assert result["details"]["router_port_up"] is False
    assert seen == ["eth1"]

    seen.clear()
    monkeypatch.setattr(link_mod, "native_fault_ops", lambda *a, **k: Ops(True))
    result = problem.verify_fault(LinkDetachParams(host_name="leaf1", intf_name="eth1"))
    assert result["verified"] is False
    assert result["details"]["router_port_up"] is True

    # XRd ports ignore Linux link state, so only the Linux check applies.
    xrd = Ops(True)
    xrd.port_follows_link = False
    monkeypatch.setattr(link_mod, "native_fault_ops", lambda *a, **k: xrd)
    result = problem.verify_fault(LinkDetachParams(host_name="leaf1", intf_name="eth1"))
    assert result["verified"] is True
    assert "router_port_up" not in result["details"]


def test_link_detach_verify_linux_only_when_no_provider(monkeypatch) -> None:
    runtime = SimpleNamespace(
        backend="containerlab",
        interface_exists=lambda host, intf: False,
    )
    problem = _problem(LinkDetach, runtime)
    monkeypatch.setattr(link_mod, "native_fault_ops", lambda *a, **k: None)
    result = problem.verify_fault(
        LinkDetachParams(host_name="client1", intf_name="eth1")
    )
    assert result["verified"] is True
    assert "router_port_up" not in result["details"]


def test_static_route_replace_skips_ra_and_emits_canonical_default() -> None:
    ra = {
        "dst": "default",
        "gateway": "fe80::1",
        "protocol": "ra",
        "metric": 1024,
        "expires": 1799,
    }
    assert static_route_replace_command("-6", ra, "eth1") is None
    kernel = {"dst": "1000:10::/64", "protocol": "kernel", "prefsrc": "1000:10::24"}
    assert static_route_replace_command("-6", kernel, "eth1") is None
    static = {
        "dst": "default",
        "gateway": "1000:10::24",
        "protocol": "boot",
        "metric": 1024,
    }
    assert (
        static_route_replace_command("-6", static, "eth1")
        == "ip -6 route replace default via 1000:10::24 metric 1024 proto boot dev eth1"
    )


def test_parse_ip_route_json_empty_and_invalid() -> None:
    assert parse_ip_route_json("") == []
    assert parse_ip_route_json("[]") == []
    assert parse_ip_route_json("not-json") is None


def test_restore_l3_state_raises_when_replace_fails() -> None:
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


def test_restore_l3_state_runs_canonical_replace() -> None:
    seen: list[str] = []

    def exec_cmd(host: str, cmd: str) -> str:
        seen.append(cmd)
        return "NIKA_ROUTE_OK"

    runtime = SimpleNamespace(backend="containerlab", exec=exec_cmd)
    problem = _problem(LinkDetach, runtime)
    problem._saved_routes = [
        "ip -6 route replace default via 1000:10::24 proto boot dev eth1"
    ]
    problem._restore_l3_state("client1", "eth1")
    assert seen == [
        "ip -6 route replace default via 1000:10::24 proto boot dev eth1 && echo NIKA_ROUTE_OK"
    ]
    assert problem._saved_routes == []
