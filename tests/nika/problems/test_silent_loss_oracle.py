"""Paced loss measurements fit tool budgets and use the lab clock."""

import shlex
import sys
from types import SimpleNamespace

import pytest

from nika.problems.link_interface.p4_gateway import (
    SilentEgressPacketLoss,
    SilentEgressPacketLossParams,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("size", ["s", "m", "l"])
@pytest.mark.parametrize("role", ["leaf", "spine"])
def test_loss_sampler_preserves_affected_flows_within_each_budget(
    size, role, monkeypatch
):
    problem = SilentEgressPacketLoss("p4_dc_gateway", topo_size=size)
    model = problem.net_env.model
    switch = model.spines[0] if role == "leaf" else model.gateways[0]
    port = next(p for p in model.ports[switch] if p.role == role)
    params = SilentEgressPacketLossParams(
        host_name=switch, intf_name=port.name, bmv2_port=port.bmv2_port
    )
    plan = problem.oracle_playbook(params)
    clock = next(s for s in plan.steps if s.save_as == "loss_probe_start")
    assert clock.expect("1794880000000000000")
    assert not clock.expect("tool_error")
    assert (
        plan.steps[-1].args({"loss_probe_start": "1794880000000000000"})["start_time"]
        == "1794880000000000000"
    )

    services = model.services_on_leaf(port.peer) if role == "leaf" else model.services
    expected_sources = (
        model.clients if role == "leaf" else [model.client_on_gateway(switch)]
    )
    observed = []
    for step in plan.steps:
        if step.purpose != "Send paced UDP probes across the affected egress.":
            continue
        argv = shlex.split(step.args["command"])
        sent, sleeps, bindings = [], [], []

        class Socket:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def bind(self, address):
                bindings.append(address)

            def sendto(self, payload, destination):
                sent.append((payload, destination))

        monkeypatch.setitem(
            sys.modules,
            "socket",
            SimpleNamespace(socket=lambda *_: Socket(), AF_INET=2, SOCK_DGRAM=2),
        )
        monkeypatch.setitem(sys.modules, "time", SimpleNamespace(sleep=sleeps.append))
        monkeypatch.setattr(sys, "argv", ["probe", *argv[3:]])
        exec(argv[2], {})
        assert sum(sleeps) < step.args["timeout"]
        assert len(sent) == len(argv[3:]) * 12 * 32
        assert len(set(bindings)) == 12
        observed.extend((step.args["host_name"], ip) for ip in argv[3:])
    assert sorted(observed) == sorted(
        (src.name, service.ip) for src in expected_sources for service in services
    )
