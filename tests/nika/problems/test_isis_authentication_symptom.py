"""One-sided Hello authentication is observable in the rejecting router's state."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika.problems.routing_control_plane import isis
from nika.validation.symptom import routing_probes

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "control",
    [
        "asymmetric",
        "healthy",
        "routes_retained",
        "query_error",
        "circuit_down",
        "baseline_error",
    ],
)
def test_authentication_requires_sustained_local_routing_loss(monkeypatch, control):
    fixture = json.loads(
        (
            Path(__file__).parents[2] / "fixtures/isis-authentication-asymmetric.json"
        ).read_text()
    )
    phase = ["healthy"]

    def execute(host, command, **kwargs):
        assert host == "amsterdam"
        if phase[0] == "query_error":
            raise RuntimeError("query failed")
        if "show isis neighbor" in command:
            return fixture[
                "healthy_neighbors" if phase[0] == "healthy" else "fault_neighbors"
            ]
        if "ip -4 route show proto isis" in command:
            return fixture[
                "healthy_routes"
                if phase[0] in {"healthy", "routes_retained"}
                else "fault_routes"
            ]
        raise AssertionError(command)

    problem = isis.IsisAuthenticationMismatch.__new__(isis.IsisAuthenticationMismatch)
    problem.runtime = SimpleNamespace(backend="kathara", exec=execute)
    problem.net_env = SimpleNamespace(machine_identities={})
    params = problem.Params(host_name="amsterdam")
    assert problem.capture_hello_routing(params)[0]
    problem._audit_isis_hello_circuits = ["eth0"]
    if control == "baseline_error":
        phase[0] = "query_error"
        assert not problem.capture_hello_routing(params)[0]
    phase[0] = (
        "asymmetric" if control in {"circuit_down", "baseline_error"} else control
    )
    clock = [0]
    monkeypatch.setattr(isis.time, "monotonic", lambda: clock[0])

    def poll(predicate, *args, **kwargs):
        assert not predicate()
        clock[0] = 11
        return predicate()

    monkeypatch.setattr(isis, "wait_until", poll)
    monkeypatch.setattr(
        routing_probes,
        "_isis_live_interfaces",
        lambda *_: {"eth0": "Down" if control == "circuit_down" else "Up"},
    )
    ok, _ = routing_probes.isis_authentication_lost(problem, params)
    assert ok is (control == "asymmetric")
    assert "amsterdam" in fixture["peer_still_up"] and "Up" in fixture["peer_still_up"]


def test_authentication_local_baseline_uses_declared_srlinux_protocol_reader():
    problem = isis.IsisAuthenticationMismatch.__new__(isis.IsisAuthenticationMismatch)
    problem.net_env = SimpleNamespace(
        machine_identities={
            "amsterdam": SimpleNamespace(capabilities=("nokia_srlinux",))
        }
    )
    problem.runtime = SimpleNamespace(
        backend="containerlab",
        lab_api=SimpleNamespace(
            srl_isis_neighbors=lambda host: [
                {"system": "peer", "intf": "ethernet-1/1", "state": "up"}
            ],
            srl_routes=lambda host, protocol: {"10.255.0.2/32"},
        ),
    )
    assert problem.capture_hello_routing(problem.Params(host_name="amsterdam"))[0]
