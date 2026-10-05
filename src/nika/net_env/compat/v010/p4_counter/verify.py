"""Startup verification signals for the p4_counter scenario."""

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


def verify_p4_counter_lab(runtime: LabRuntime, *, scenario_name: str) -> dict[str, Any]:
    expected = ("pc1", "pc2", "pc3", "s1", "s2", "s3", "s4")
    checks = {
        "nodes_deployed": nodes_deployed(runtime, expected),
        "p4_switches_ready": p4_switches_ready(runtime, ("s1", "s2", "s3", "s4")),
        "pc1_ipv4": host_has_ipv4(runtime, "pc1", "10.0.0.1"),
        "pc2_ipv4": host_has_ipv4(runtime, "pc2", "10.0.0.2"),
        "pc3_ipv4": host_has_ipv4(runtime, "pc3", "10.0.0.3"),
        "pc1_to_pc2_reachable": ping_ok(runtime, "pc1", "10.0.0.2"),
        "pc1_to_pc3_reachable": ping_ok(runtime, "pc1", "10.0.0.3"),
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
    )
