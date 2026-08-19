"""Startup verification signals for the unified DC Clos scenario."""

from __future__ import annotations

from typing import Any, Literal

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


def verify_dc_clos_lab(
    runtime: LabRuntime,
    *,
    scenario_name: str,
    workload: Literal["host", "service"] = "host",
) -> dict[str, Any]:
    if workload == "service":
        return _verify_service(runtime, scenario_name=scenario_name)
    return _verify_host(runtime, scenario_name=scenario_name)


def verify_dc_clos_bgp_lab(
    runtime: LabRuntime, *, scenario_name: str
) -> dict[str, Any]:
    """Backward-compatible alias for host-workload verification."""
    return verify_dc_clos_lab(runtime, scenario_name=scenario_name, workload="host")


def verify_dc_clos_service_lab(
    runtime: LabRuntime, *, scenario_name: str
) -> dict[str, Any]:
    """Backward-compatible alias for service-workload verification."""
    return verify_dc_clos_lab(runtime, scenario_name=scenario_name, workload="service")


def _verify_host(runtime: LabRuntime, *, scenario_name: str) -> dict[str, Any]:
    expected = (
        "super_spine_router_0",
        "spine_router_0_0",
        "spine_router_0_1",
        "leaf_router_0_0",
        "leaf_router_0_1",
        "pc_0_0",
        "pc_0_1",
    )
    checks = {
        "nodes_deployed": nodes_deployed(runtime, expected),
        "super_spine_bgp_established": frr_bgp_established(
            runtime, "super_spine_router_0", min_neighbors=2
        ),
        "leaf_bgp_established": frr_bgp_established(runtime, "leaf_router_0_0"),
        "pc_0_0_ipv4": host_has_ipv4(runtime, "pc_0_0", "10.0.0.2"),
        "pc_0_1_ipv4": host_has_ipv4(runtime, "pc_0_1", "10.0.1.2"),
        "pc_gateway_reachable": ping_ok(runtime, "pc_0_0", "10.0.0.1"),
        "cross_leaf_host_reachable": ping_ok(runtime, "pc_0_0", "10.0.1.2"),
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
    )


def _verify_service(runtime: LabRuntime, *, scenario_name: str) -> dict[str, Any]:
    expected = (
        "super_spine_router_0",
        "spine_router_0_0",
        "leaf_router_0_0",
        "dns_pod0",
        "webserver0_pod0",
        "client_0",
    )
    checks = {
        "nodes_deployed": nodes_deployed(runtime, expected),
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
