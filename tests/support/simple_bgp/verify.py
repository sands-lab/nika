"""Startup verification signals for the simple_bgp scenario."""

from __future__ import annotations

from typing import Any

from nika.net_env.verify import (
    build_lab_verify_result,
    default_route_via,
    frr_bgp_established,
    host_has_ipv4,
    nodes_deployed,
    ping_ok,
    service_active,
)
from nika.runtime.base import LabRuntime


def verify_simple_bgp_lab(runtime: LabRuntime, *, scenario_name: str) -> dict[str, Any]:
    expected = ("router1", "router2", "pc1", "pc2")
    checks = {
        "nodes_deployed": nodes_deployed(runtime, expected),
        "router1_frr_active": service_active(runtime, "router1", "frr"),
        "router2_frr_active": service_active(runtime, "router2", "frr"),
        "router1_bgp_established": frr_bgp_established(runtime, "router1"),
        "pc1_ipv4": host_has_ipv4(runtime, "pc1", "195.11.14.2"),
        "pc2_ipv4": host_has_ipv4(runtime, "pc2", "200.1.1.2"),
        "pc1_default_route": default_route_via(runtime, "pc1", "195.11.14.1"),
        "pc1_gateway_reachable": ping_ok(runtime, "pc1", "195.11.14.1"),
        "pc1_to_pc2_reachable": ping_ok(runtime, "pc1", "200.1.1.2"),
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
    )
