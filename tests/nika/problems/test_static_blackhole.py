"""Blackhole injection selects a prefix carrying a declared endpoint."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nika.problems.forwarding_encapsulation_policy.static_route import (
    StaticBlackHole,
    StaticBlackHoleParams,
)
from nika.runtime.spec import NodeIdentity, NodeRole

pytestmark = pytest.mark.unit


def test_unused_bgp_aggregate_does_not_hide_the_endpoint_prefix(monkeypatch):
    problem = object.__new__(StaticBlackHole)
    problem.net_env = SimpleNamespace(
        hosts=[],
        servers={},
        machine_identities={
            "local": NodeIdentity(NodeRole.INFRASTRUCTURE, reachability_target=True),
            "remote": NodeIdentity(NodeRole.INFRASTRUCTURE, reachability_target=True),
        },
    )
    problem.runtime = SimpleNamespace(
        backend="kathara",
        get_connected_devices=lambda _: ["local"],
        get_host_ip=lambda host, **_: {
            "local": "201.1.1.2/24",
            "remote": "201.2.1.2/24",
        }[host],
        exec=Mock(
            return_value="101.0.0.0/8 via 10.0.0.1\n201.0.0.0/8 via 10.0.0.1\n201.2.1.0/24 via 10.0.0.1\n"
        ),
    )
    monkeypatch.setattr(
        "nika.problems.forwarding_encapsulation_policy.static_route.native_fault_ops",
        lambda *_: None,
    )
    params = StaticBlackHoleParams(host_name="router")
    assert problem.attached_hosts(params) == ["local"]
    assert problem.blackhole_network(params) == "201.2.1.0/24"
