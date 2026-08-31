"""Startup verification signals for the unified DC Clos scenario."""

from __future__ import annotations

from typing import Any

from nika.net_env.verify import (
    build_lab_verify_result,
    frr_bgp_established,
    host_has_ipv4,
    http_ok,
    nodes_deployed,
    ping_ok,
    service_active,
)
from nika.runtime.base import LabRuntime

_SERVICE_EXPECTED = (
    "super_spine_router_0",
    "spine_router_0_0",
    "leaf_router_0_0",
    "dns_pod0",
    "webserver0_pod0",
    "client_0",
)


def verify_dc_clos_lab_startup(
    runtime: LabRuntime,
    *,
    scenario_name: str,
) -> dict[str, Any]:
    checks = {
        "nodes_deployed": nodes_deployed(runtime, _SERVICE_EXPECTED),
        "super_spine_bgp_established": frr_bgp_established(
            runtime, "super_spine_router_0"
        ),
        "client_ipv4": host_has_ipv4(runtime, "client_0", "192.168.0.2"),
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
    )


def verify_dc_clos_lab(
    runtime: LabRuntime,
    *,
    scenario_name: str,
) -> dict[str, Any]:
    checks = {
        "nodes_deployed": nodes_deployed(runtime, _SERVICE_EXPECTED),
        "super_spine_bgp_established": frr_bgp_established(
            runtime, "super_spine_router_0"
        ),
        "client_ipv4": host_has_ipv4(runtime, "client_0", "192.168.0.2"),
        "dns_reachable": ping_ok(runtime, "client_0", "10.0.0.2"),
        "web_reachable": ping_ok(runtime, "client_0", "10.0.1.2"),
        "dns_service_active": service_active(runtime, "dns_pod0", "named"),
        "web_http": http_ok(runtime, "client_0", "http://web0.pod0/"),
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
    )
