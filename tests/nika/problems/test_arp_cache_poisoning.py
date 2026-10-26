"""Recheck ARP poisoning from the neighbor stored at inject time."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nika.problems.security.arp import ArpCachePoisoning, ArpCachePoisoningParams

pytestmark = pytest.mark.unit

_FAKE_MAC = "00:11:22:33:44:55"
_GATEWAY = "10.0.0.1"
_TIMEOUT = "[TIMEOUT] Command 'ip -j route' on 'client' exceeded 10s."


class _ArpRuntime:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.timeout_routes = False

    def exec(self, node: str, cmd: str, *, timeout: float = 10.0) -> str:
        self.calls.append(cmd)
        if cmd.startswith("ip -j route"):
            if self.timeout_routes:
                return _TIMEOUT
            return json.dumps([{"dst": "default", "gateway": _GATEWAY, "dev": "eth0"}])
        if "ip neigh show" in cmd:
            return f"{_GATEWAY} dev eth0 lladdr {_FAKE_MAC} PERMANENT"
        return ""


def test_verify_uses_neighbor_from_inject_when_route_lookup_times_out() -> None:
    runtime = _ArpRuntime()
    problem = ArpCachePoisoning(None)
    problem.runtime = runtime
    problem.net_env = SimpleNamespace(ROUTERS_CHECK_DST_MAC=False)
    params = ArpCachePoisoningParams(host_name="client", fake_mac=_FAKE_MAC)
    problem.inject_fault(params)
    runtime.timeout_routes = True
    runtime.calls.clear()

    result = problem.verify_fault(params)

    assert result["verified"] is True
    assert result["details"]["gateway"] == _GATEWAY
    assert not any(cmd.startswith("ip -j route") for cmd in runtime.calls)
