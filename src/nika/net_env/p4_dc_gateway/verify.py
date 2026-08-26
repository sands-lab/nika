"""Post-deploy checks for the gateway P4 fabric."""

from __future__ import annotations

import json
from typing import Any

from nika.net_env.p4_dc_fabric.fabric_manager.apply import run_manager
from nika.net_env.p4_dc_gateway.topology_model import GatewayFabricModel
from nika.net_env.verify import (
    build_lab_verify_result,
    exec_or_empty,
    http_ok,
    nodes_deployed,
    process_running,
)
from nika.runtime.base import LabRuntime


def _p4runtime_consistent(
    observed: dict, model: GatewayFabricModel
) -> tuple[bool, dict[str, Any]]:
    live = observed.get("switches") or {}
    details: dict[str, Any] = {}
    ok = bool(observed.get("ok")) and set(live) == set(model.fabric_switches())
    for name in model.fabric_switches():
        switch = live.get(name) or {}
        pipeline = switch.get("pipeline") or {}
        mismatches = switch.get("mismatches") or []
        row = {
            "pipeline_ok": bool(pipeline.get("ok")),
            "mismatches": mismatches,
            "ipv4_lpm": len(switch.get("ipv4_lpm") or []),
            "groups": len(switch.get("groups") or []),
            "members": len(switch.get("members") or []),
        }
        details[name] = row
        if (
            not row["pipeline_ok"]
            or mismatches
            or not row["ipv4_lpm"]
            or not row["groups"]
            or not row["members"]
        ):
            ok = False
    return ok, details


def _valid_hop(hop: object) -> bool:
    if not isinstance(hop, dict):
        return False
    required = {
        "switch_id",
        "ingress_port",
        "egress_port",
        "ingress_timestamp",
        "egress_timestamp",
        "hop_latency",
        "queue_occupancy",
        "ecn",
        "m",
        "e",
    }
    return required <= hop.keys()


def _collector_telemetry(
    runtime: LabRuntime, model: GatewayFabricModel
) -> tuple[bool, dict[str, Any]]:
    client = model.clients[0]
    service = model.services[0]
    raw = exec_or_empty(
        runtime,
        "collector",
        "tail -n 2000 /var/lib/nika/int_reports.jsonl 2>/dev/null || true",
        timeout=20,
    )
    candidates: list[dict] = []
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            protocol = int(record.get("protocol", -1))
            dst_port = int(record.get("dst_port", -1))
        except (TypeError, ValueError):
            continue
        if (
            record.get("src") == client.ip
            and record.get("dst") == service.ip
            and protocol == 6
            and dst_port == 80
        ):
            candidates.append(record)

    gateway_ids = {model.switch_info[name].device_id for name in model.gateways}
    spine_ids = {model.switch_info[name].device_id for name in model.spines}
    leaf_ids = {model.switch_info[name].device_id for name in model.leaves}
    best = max(
        candidates, key=lambda row: len(row.get("hop_sequence") or []), default={}
    )
    hops = best.get("hop_sequence") or []
    observed_ids = {int(hop["switch_id"]) for hop in hops if _valid_hop(hop)}
    roles_seen = {
        "gateway": bool(observed_ids & gateway_ids),
        "spine": bool(observed_ids & spine_ids),
        "leaf": bool(observed_ids & leaf_ids),
    }
    complete = (
        bool(best.get("sink_seen"))
        and bool(best.get("trace_complete"))
        and len(hops) >= 3
        and all(_valid_hop(hop) for hop in hops)
        and all(roles_seen.values())
        and int(best.get("packet_timestamp") or 0) > 0
        and bool(best.get("flow_id"))
        and bool(best.get("packet_id"))
    )
    return complete, {
        "source": client.ip,
        "destination": service.ip,
        "candidate_records": len(candidates),
        "hop_count": len(hops),
        "switch_ids": sorted(observed_ids),
        "roles_seen": roles_seen,
        "sink_seen": bool(best.get("sink_seen")),
        "trace_complete": bool(best.get("trace_complete")),
    }


def _emit_int_probe(runtime: LabRuntime, model: GatewayFabricModel) -> None:
    """Send a non-VIP TCP probe that exercises the INT watchlist.

    VIP traffic intentionally avoids INT encapsulation because its L4 checksum
    is part of the gateway load-balancing contract.  This probe therefore uses
    the service address directly and is only an observation packet; the normal
    VIP HTTP request remains the service-health check.
    """
    client = model.clients[0]
    service = model.services[0]
    exec_or_empty(
        runtime,
        client.name,
        f"nc -z -w 1 {service.ip} 80 || true",
        timeout=5,
    )


def verify_p4_dc_gateway_lab(
    runtime: LabRuntime, scenario_name: str, model: GatewayFabricModel
) -> dict:
    expected = model.fabric_switches() + [item.name for item in model.endpoints]
    expected += ["fabric_mgr", "collector"]
    switches_up = all(
        process_running(runtime, switch, "simple_switch_grpc")
        for switch in model.fabric_switches()
    )
    try:
        observed = run_manager(runtime, "read", timeout=180)
    except Exception:
        observed = {}
    p4runtime, p4runtime_details = _p4runtime_consistent(observed, model)
    # Backend DIPs are intentionally internal.  The externally reachable
    # healthy workload is the stateful VIP, so this check exercises both
    # directions of the L4 translation rather than bypassing it.
    http = all(http_ok(runtime, client.name, model.vip_url) for client in model.clients)
    collector_process = process_running(runtime, "collector", "python3")
    _emit_int_probe(runtime, model)
    telemetry_ok, telemetry_details = _collector_telemetry(runtime, model)
    checks = {
        "nodes": nodes_deployed(runtime, expected),
        "bmv2": switches_up,
        "p4runtime": p4runtime,
        "client_to_service_http": http,
        "collector_process": collector_process,
        "int_telemetry": telemetry_ok,
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
        details={
            "dimensions": {
                "gateways": model.gateway_count,
                "spines": model.spine_count,
                "leaves": model.leaf_count,
                "clients": model.client_count,
                "services": model.service_count,
            },
            "gateway_spine_links": len(model.gateway_spine_links),
            "spine_leaf_links": len(model.spine_leaf_links),
            "p4runtime": p4runtime_details,
            "int_telemetry": telemetry_details,
        },
    )
