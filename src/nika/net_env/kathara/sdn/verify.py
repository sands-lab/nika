"""Startup verification signals for SDN scenarios."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from nika.net_env.verify import (
    build_lab_verify_result,
    exec_or_empty,
    host_has_ipv4,
    link_up,
    nodes_deployed,
    ping_ok,
    process_running,
)
from nika.runtime.base import LabRuntime


def _ovs_ready(runtime: LabRuntime, switches: Iterable[str]) -> bool:
    return all(
        bool(exec_or_empty(runtime, switch, "ovs-vsctl show").strip())
        for switch in switches
    )


def verify_sdn_star_lab(runtime: LabRuntime, *, scenario_name: str) -> dict[str, Any]:
    expected = ("controller", "switch_0", "switch_1", "pc1", "pc2")
    checks = {
        "nodes_deployed": nodes_deployed(runtime, expected),
        "controller_link_up": link_up(runtime, "controller"),
        "controller_process": process_running(runtime, "controller", "python3"),
        "ovs_switches_ready": _ovs_ready(runtime, ("switch_0", "switch_1")),
        "pc1_ipv4": host_has_ipv4(runtime, "pc1", "10.0.0.1"),
        "pc2_ipv4": host_has_ipv4(runtime, "pc2", "10.0.0.2"),
        "host_to_host_reachable": ping_ok(runtime, "pc1", "10.0.0.2"),
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
    )


def verify_sdn_clos_lab(
    runtime: LabRuntime,
    *,
    scenario_name: str,
    host_ips: dict[str, str] | None = None,
) -> dict[str, Any]:
    # Host addresses are assigned sequentially across leaves, so pc_2_1's IP
    # depends on hosts-per-leaf: 10.0.0.3 on "s" but 10.0.0.5/.9 on "m"/"l".
    ips = host_ips or {"pc_1_1": "10.0.0.1", "pc_2_1": "10.0.0.3"}
    expected = ("controller", "spine_1", "leaf_1", "leaf_2", "pc_1_1", "pc_2_1")
    checks = {
        "nodes_deployed": nodes_deployed(runtime, expected),
        "controller_link_up": link_up(runtime, "controller"),
        "controller_process": process_running(runtime, "controller", "python3"),
        "ovs_switches_ready": _ovs_ready(runtime, ("spine_1", "leaf_1", "leaf_2")),
        "pc_1_1_ipv4": host_has_ipv4(runtime, "pc_1_1", ips["pc_1_1"]),
        "pc_2_1_ipv4": host_has_ipv4(runtime, "pc_2_1", ips["pc_2_1"]),
        "cross_leaf_host_reachable": ping_ok(runtime, "pc_1_1", ips["pc_2_1"]),
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
    )
