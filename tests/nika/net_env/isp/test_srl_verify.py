"""SR Linux ISP adjacency parsing against real sr_cli table output (no Docker)."""

from __future__ import annotations

import pytest

from nika.net_env.isp.containerlab.verify import _igp_adjacencies_ok
from nika.net_env.isp.igp import IspConfig, compile_isp_plan
from nika.topology.models import NetworkTopology, TopoLink, TopoNode

pytestmark = pytest.mark.unit


def _plan(igp: str):
    topology = NetworkTopology(
        name="tiny",
        source_format="sndlib-xml",
        meta={},
        nodes=(TopoNode(id="A"), TopoNode(id="B")),
        links=(TopoLink(id="L1", source="A", target="B"),),
        demands=(),
    )
    return compile_isp_plan(IspConfig(topology="polska", igp=igp), topology=topology)


class _Runtime:
    def __init__(self, rows: dict[str, str]) -> None:
        self.rows = rows

    def exec(self, host: str, command: str, timeout: float = 10.0) -> str:
        return self.rows[host]


def _ospf_row(peer_router_id: str, state: str) -> str:
    # OSPF neighbor rows box the whole row, not each column.
    return (
        "| Interface-Name         Rtr Id            State        Pri   RetxQ |\n"
        f"| ethernet-1/1.0         {peer_router_id:<17} {state:<12} 1     0     |\n"
    )


def _isis_row(peer_address: str, state: str) -> str:
    return (
        "| Interface Name | Neighbor System Id | Adjacency Level | Ip Address "
        "| Ipv6 Address | State |\n"
        f"| ethernet-1/1.0 | 0000.0AFF.0002 | L2 | {peer_address} | :: | {state} |\n"
    )


def _outputs(plan, row) -> dict[str, str]:
    a, b = plan.nodes
    iface_a, iface_b = a.interfaces[0], b.interfaces[0]
    if plan.igp == "ospf":
        return {a.device_name: row(b.router_id), b.device_name: row(a.router_id)}
    return {
        a.device_name: row(iface_a.peer_address),
        b.device_name: row(iface_b.peer_address),
    }


@pytest.mark.parametrize(
    ("igp", "row", "up", "down"),
    [
        ("ospf", _ospf_row, "full", "two-way"),
        ("isis", _isis_row, "up", "down"),
    ],
)
def test_adjacency_requires_full_or_up_state(igp, row, up, down) -> None:
    plan = _plan(igp)
    healthy = _outputs(plan, lambda peer: row(peer, up))
    broken = dict(healthy)
    first = plan.nodes[0].device_name
    broken[first] = _outputs(plan, lambda peer: row(peer, down))[first]

    assert _igp_adjacencies_ok(_Runtime(healthy), plan)
    assert not _igp_adjacencies_ok(_Runtime(broken), plan)


def test_adjacency_must_be_with_the_planned_peer() -> None:
    plan = _plan("isis")
    wrong_peer = _outputs(plan, lambda peer: _isis_row("192.0.2.99", "up"))

    assert not _igp_adjacencies_ok(_Runtime(wrong_peer), plan)
