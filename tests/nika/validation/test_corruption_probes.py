"""Kernel egress matching for forwarding-corruption probes (no Docker)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nika.runtime.spec import NodeRole
from nika.validation.symptom import corruption_probes as cp

pytestmark = pytest.mark.unit


def _env(*, sonic: bool) -> SimpleNamespace:
    profile = "sonic" if sonic else "nokia_srlinux"
    cap = ("linux", "sonic" if sonic else "srl", "bgp")
    router_ports = ("eth1", "eth2") if sonic else ("e1-1", "e1-2")
    identities = {
        "leaf1": SimpleNamespace(role=NodeRole.ROUTER, capabilities=cap),
        "client1": SimpleNamespace(role=NodeRole.HOST, capabilities=("linux",)),
        "client2": SimpleNamespace(role=NodeRole.HOST, capabilities=("linux",)),
    }
    return SimpleNamespace(
        machine_identities=identities,
        inventory={
            "links": [
                {
                    "endpoint_a": {"device": "leaf1", "iface": router_ports[0]},
                    "endpoint_b": {"device": "client1", "iface": "eth1"},
                },
                {
                    "endpoint_a": {"device": "leaf1", "iface": router_ports[1]},
                    "endpoint_b": {"device": "client2", "iface": "eth1"},
                },
            ]
        },
        backend="containerlab",
        device_profile=profile,
        topo_size="s",
        lab=None,
    )


def _problem(net_env: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(),
        net_env=net_env,
        scenario_name="min3clos",
    )


@pytest.mark.parametrize(
    ("sonic", "intf", "route", "keep"),
    [
        (True, "eth1", "10.0.2.2 via 10.0.1.1 dev Ethernet0 src 10.0.1.2", True),
        (True, "eth1", "10.0.2.2 via 10.0.1.1 dev Ethernet4 src 10.0.1.2", False),
        (False, "e1-1", "10.0.2.2 via 10.0.0.1 dev eth0 src 10.0.0.2", True),
    ],
)
def test_crossing_ends_matches_sonic_kernel_egress(
    monkeypatch, sonic: bool, intf: str, route: str, keep: bool
) -> None:
    problem = _problem(_env(sonic=sonic))
    params = SimpleNamespace(forwarding_device="leaf1", intf_name=intf)
    declared = {"src": "client1", "dst": "10.0.2.2", "dst_host": "client2"}
    monkeypatch.setattr(cp, "_ends", lambda *_a, **_k: declared)
    monkeypatch.setattr(
        cp,
        "exec_or_empty",
        lambda _rt, _node, cmd, **_k: route if "route get" in cmd else "",
    )
    monkeypatch.setattr(
        cp,
        "lab_addresses",
        lambda _rt: {
            "leaf1": [("eth1", "10.0.1.1/24")],
            "client1": [("eth1", "10.0.1.2/24")],
            "client2": [("eth1", "10.0.2.2/24")],
        },
    )
    ends = cp._crossing_ends(problem, params)
    if keep:
        assert ends == declared
    else:
        assert ends.get("error") == "no_end_host_path_leaves_the_device_port"
