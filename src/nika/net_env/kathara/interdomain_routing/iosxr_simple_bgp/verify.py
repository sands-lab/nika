"""Startup verification signals for the iosxr_simple_bgp scenario."""

from __future__ import annotations

import re
from typing import Any

from nika.net_env.kathara.interdomain_routing.iosxr_simple_bgp.lab import LINK_IFACE
from nika.net_env.verify import (
    build_lab_verify_result,
    default_route_via,
    exec_or_empty,
    host_has_ipv4,
    nodes_deployed,
    ping_ok,
)
from nika.runtime.base import LabRuntime


def _iosxr_interface_up(runtime: LabRuntime, router: str, interface: str) -> bool:
    output = exec_or_empty(runtime, router, "/pkg/bin/xr_cli 'show ip interface brief'")
    pattern = re.compile(rf"{re.escape(interface)}\s+\S+\s+Up\s+Up")
    return bool(pattern.search(output))


def _iosxr_bgp_established(
    runtime: LabRuntime, router: str, *, min_neighbors: int = 1
) -> bool:
    output = exec_or_empty(
        runtime, router, "/pkg/bin/xr_cli 'show bgp summary'", timeout=20
    )
    established = 0
    for line in output.splitlines():
        fields = line.split()
        if fields and fields[-1].isdigit():
            established += 1
    return established >= min_neighbors


def verify_iosxr_simple_bgp_lab_startup(
    runtime: LabRuntime, *, scenario_name: str
) -> dict[str, Any]:
    expected = ("router1", "router2", "pc1", "pc2")
    checks = {
        "nodes_deployed": nodes_deployed(runtime, expected),
        "router1_bgp_established": _iosxr_bgp_established(runtime, "router1"),
        "pc1_gateway_reachable": ping_ok(runtime, "pc1", "195.11.14.1"),
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
    )


def verify_iosxr_simple_bgp_lab(
    runtime: LabRuntime, *, scenario_name: str
) -> dict[str, Any]:
    expected = ("router1", "router2", "pc1", "pc2")
    checks = {
        "nodes_deployed": nodes_deployed(runtime, expected),
        "router1_iface_up": _iosxr_interface_up(runtime, "router1", LINK_IFACE),
        "router2_iface_up": _iosxr_interface_up(runtime, "router2", LINK_IFACE),
        "router1_bgp_established": _iosxr_bgp_established(runtime, "router1"),
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
