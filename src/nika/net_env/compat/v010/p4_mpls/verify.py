"""Startup verification signals for the p4_mpls scenario."""

from __future__ import annotations

from typing import Any

from nika.net_env.compat.v010.p4_verify import p4_switches_ready
from nika.net_env.verify import (
    build_lab_verify_result,
    host_has_ipv4,
    nodes_deployed,
    ping_ok,
)
from nika.runtime.base import LabRuntime


def verify_p4_mpls_lab(runtime: LabRuntime, *, scenario_name: str) -> dict[str, Any]:
    switches = tuple(f"switch_{idx}" for idx in range(1, 8))
    checks = {
        "nodes_deployed": nodes_deployed(runtime, ("pc1", "pc2", "pc3", *switches)),
        "p4_switches_ready": p4_switches_ready(runtime, switches),
        "pc1_ipv4": host_has_ipv4(runtime, "pc1", "10.1.1.2"),
        "pc2_ipv4": host_has_ipv4(runtime, "pc2", "10.7.2.2"),
        "pc3_ipv4": host_has_ipv4(runtime, "pc3", "10.7.3.2"),
        "pc1_to_pc2_reachable": ping_ok(runtime, "pc1", "10.7.2.2"),
        "pc1_to_pc3_reachable": ping_ok(runtime, "pc1", "10.7.3.2"),
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
    )
