"""Unit tests for controller-side link faults (no Docker)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nika.problems.link_interface.link import (
    LinkCapacityBottleneck,
    LinkCapacityBottleneckParams,
    LinkFailure,
    LinkFailureParams,
    LinkFlap,
    LinkFlapParams,
)
from nika.runtime.base import RuntimeCapabilityError
import nika.problems.link_interface.link as link_mod
from nika.workflows.failure.inject import _refresh_injection_params


@pytest.mark.unit
@pytest.mark.parametrize(
    ("carrier", "received", "expected"),
    [
        ("000111" * 4, 0, True),
        ("0" * 24, 0, False),
        ("1" * 24, 0, False),
        ("000111" * 2, 0, False),
        ("???111" + "000111" * 3, 0, False),
        ("000111" * 4, 5, False),
        ("", 0, False),
    ],
)
def test_flap_oracle_requires_carrier_cycles_for_sustained_loss(
    carrier, received, expected
):
    samples = " ".join(f"carrier={state}" for state in carrier)
    output = f"{samples} 24 packets transmitted, {received} received"
    assert link_mod._flap_loss_or_carrier(output) is expected


@pytest.mark.unit
def test_flap_oracle_still_accepts_distinct_loss_bursts():
    output = "icmp_seq=1 icmp_seq=5 icmp_seq=9\n9 packets transmitted, 3 received"
    assert link_mod._flap_loss_or_carrier(output)


pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "states,expected",
    [
        (["down"] * 4, True),
        (["down", "up", "down", "down"], False),
        (["up"] * 4, False),
        (["down", "unknown", "down", "down"], False),
    ],
)
def test_link_down_requires_sustained_live_carrier_loss(monkeypatch, states, expected):
    from nika.problems.rca.inventory import load_offline_net_env

    now = [0.0]
    monkeypatch.setattr(link_mod.time, "monotonic", lambda: now[0])
    problem = LinkFailure.__new__(LinkFailure)
    problem.net_env = load_offline_net_env("dc_clos", "s")
    step = problem.oracle_playbook(LinkFailureParams(host_name="client_1_1")).steps[0]
    for index, state in enumerate(states):
        now[0] = index * 4.0
        confirmed = step.expect(f"state {state.upper()}")
        if index < 3:
            assert not confirmed
    assert confirmed is expected


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


@pytest.mark.parametrize("profile", ["frr", "iosxr", "routeros"])
def test_capacity_oracle_measures_between_end_hosts(profile) -> None:
    problem = LinkCapacityBottleneck(
        "enterprise_branch", topo_size="s", device_profile=profile
    )
    playbook = problem.oracle_playbook(
        LinkCapacityBottleneckParams(host_name="isp2_core", intf_name="eth2")
    )
    evidence = next(step for step in playbook.steps if step.evidence)
    args = evidence.args({"cap_peer": "2: eth0 inet 10.1.0.2/24"})
    assert args["host_name"] == "br2_corp_pc"
    assert "iperf3 -c 10.1.0.2" in args["command"]
    address = next(step for step in playbook.steps if step.save_as == "cap_peer")
    assert address.args["host_name"] == "br1_corp_pc"
    assert "dev eth0" in address.args["command"]
    assert evidence.expect(
        "500 Kbits/sec receiver\nrtt min/avg/max/mdev = 90.0/150.0/200.0/10.0 ms"
    )
    for output in (
        "ping: connect: Network is unreachable",
        "500 Kbits/sec receiver\nrtt min/avg/max/mdev = 1.0/2.0/3.0/1.0 ms",
        "5000 Kbits/sec receiver\nrtt min/avg/max/mdev = 90.0/150.0/200.0/10.0 ms",
    ):
        assert not evidence.expect(output)


@pytest.mark.parametrize("profile", ["iosxr", "routeros"])
def test_capacity_oracle_rejects_native_shell_without_host_path(profile) -> None:
    net_env = SimpleNamespace(
        backend="kathara",
        inventory={
            "links": [
                {
                    "endpoint_a": {"device": "r1", "iface": "eth0"},
                    "endpoint_b": {"device": "r2", "iface": "eth0"},
                }
            ]
        },
        machine_identities={
            name: SimpleNamespace(role="router", capabilities=("linux", profile))
            for name in ("r1", "r2")
        },
    )
    problem = _problem(
        LinkCapacityBottleneck,
        SimpleNamespace(backend="kathara"),
        net_env=net_env,
    )
    with pytest.raises(ValueError, match="no addressed probe path"):
        problem.oracle_playbook(LinkCapacityBottleneckParams(host_name="r1"))
