"""Locally administered VTEP MAC for the overlay neighbor failure."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nika.problems.forwarding_encapsulation_policy.kubernetes_overlay import (
    VxlanNeighborStale,
    VxlanNeighborStaleParams,
    previous_vtep_mac,
)

pytestmark = pytest.mark.unit

_PEER_VTEP = "10.42.7.0"
_PEER_MAC = "3a:a7:af:50:7e:63"
_NODE_MAC = "2a:19:1a:1b:ee:ab"
_OTHER_MAC = "4e:11:22:33:44:55"


def test_previous_vtep_mac_is_locally_administered_unicast() -> None:
    mac = previous_vtep_mac(_PEER_MAC, {_PEER_MAC})
    assert mac == previous_vtep_mac(_PEER_MAC, {_PEER_MAC})
    assert mac != _PEER_MAC
    first = int(mac[:2], 16)
    assert first & 0x01 == 0
    assert first & 0x02 == 0x02


class _OverlayRuntime:
    def __init__(self) -> None:
        self.macs = {
            "worker1": _NODE_MAC,
            "worker2": _PEER_MAC,
            "worker3": _OTHER_MAC,
        }
        self.inets = {
            "worker1": "10.42.6.0",
            "worker2": _PEER_VTEP,
            "worker3": "10.42.1.0",
        }
        self.neigh = {_PEER_VTEP: _PEER_MAC}
        self.replaced: list[str] = []
        self.k8s = True

    def require_capabilities(self, *names: str) -> None:
        del names

    def exec(self, node: str, cmd: str, *, timeout: float = 10.0) -> str:
        del timeout
        if cmd.startswith("ip -d link show"):
            return "vxlan id 1 local 201.1.2.2 dev eth0 dstport 8472"
        if "ip -4 addr show" in cmd:
            return f"inet {self.inets[node]}/32"
        if "ip -o link show" in cmd:
            return f"link/ether {self.macs[node]}"
        if "ip neigh show" in cmd:
            vtep = _PEER_VTEP
            if " to " in cmd:
                vtep = cmd.split(" to ", 1)[1].split()[0]
            mac = self.neigh.get(vtep, "")
            return f"{vtep} lladdr {mac} PERMANENT" if mac else ""
        if cmd.startswith("ip neigh replace"):
            self.replaced.append(cmd)
            parts = cmd.split()
            self.neigh[parts[3]] = parts[5]
            return ""
        return ""


def test_inject_skips_macs_already_on_flannel() -> None:
    runtime = _OverlayRuntime()
    problem = VxlanNeighborStale(None)
    problem.runtime = runtime
    problem.net_env = SimpleNamespace(
        LAB_NAME="k8s_lab",
        kubernetes_nodes=["worker1", "worker2", "worker3"],
    )
    params = VxlanNeighborStaleParams(node_name="worker1", peer_name="worker2")
    colliding = previous_vtep_mac(_PEER_MAC, set())
    runtime.macs["worker3"] = colliding

    problem.inject_fault(params)

    assert problem.injected_mac not in {_PEER_MAC, _NODE_MAC, colliding}
    assert problem.injected_mac in runtime.replaced[-1]
    assert "nud permanent" in runtime.replaced[-1]
