"""Startup verification for the 0.1.0 POX SDN labs."""

from __future__ import annotations

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


def verify_sdn_lab(
    runtime: LabRuntime,
    *,
    scenario_name: str,
    switches: tuple[str, ...],
    hosts: dict[str, str],
) -> dict[str, Any]:
    (src, _), (dst, dst_ip) = list(hosts.items())[:2]
    checks = {
        "nodes_deployed": nodes_deployed(runtime, ("controller", *switches, *hosts)),
        "controller_link_up": link_up(runtime, "controller"),
        "controller_process": process_running(runtime, "controller", "python3"),
        "ovs_switches_ready": all(
            bool(exec_or_empty(runtime, sw, "ovs-vsctl show").strip())
            for sw in switches
        ),
        **{f"{h}_ipv4": host_has_ipv4(runtime, h, ip) for h, ip in hosts.items()},
        "host_to_host_reachable": ping_ok(runtime, src, dst_ip),
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
    )
