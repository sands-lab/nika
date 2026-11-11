"""Scenario-neutral helpers for P4Runtime forwarding failures."""

from __future__ import annotations

import ipaddress
import json
import shlex
from pathlib import Path
from typing import Any

from nika.runtime.base import LabRuntime


def run_manager(
    runtime: LabRuntime, *args: str, timeout: float = 90.0, **kwargs: str
) -> dict[str, Any]:
    """Run the shared P4Runtime manager against the live scenario intent."""
    from nika.net_env.p4_dc_fabric.fabric_manager.apply import (
        run_manager as shared_run_manager,
    )

    return shared_run_manager(runtime, *args, timeout=timeout, **kwargs)


def load_intent(runtime: LabRuntime) -> dict[str, Any]:
    """Read the intent installed by either supported P4 scenario."""
    from nika.net_env.p4_dc_fabric.fabric_manager.apply import (
        load_intent as shared_load_intent,
    )

    return shared_load_intent(runtime)


def lpm_prefix(intent: dict[str, Any], switch: str) -> str | None:
    """Choose a routed prefix, preferring a multi-path group when present."""
    entries = intent["switches"][switch].get("ipv4_lpm") or []
    groups = {
        int(group["group_id"]): group
        for group in intent["switches"][switch].get("groups") or []
    }
    for entry in entries:
        group = groups.get(int(entry["group_id"]))
        if group and len(group.get("member_ids") or []) > 1:
            return str(entry["prefix"])
    return str(entries[0]["prefix"]) if entries else None


def other_group_id(intent: dict[str, Any], switch: str, prefix: str) -> int | None:
    """Choose a different programmed group for an LPM misconfiguration."""
    entries = intent["switches"][switch].get("ipv4_lpm") or []
    return next(
        (int(entry["group_id"]) for entry in entries if str(entry["prefix"]) != prefix),
        None,
    )


def probe_victim_ip(intent: dict[str, Any]) -> str:
    """Return the cross-rack web IP used by the default p4_dc_fabric probe path."""
    from nika.net_env.p4_dc_fabric.topology_model import build_clos_fabric_model

    topo_size = intent.get("topo_size", "s")
    model = build_clos_fabric_model(topo_size)
    observer = model.client_endpoints()[0]
    victim = next(
        web for web in model.web_endpoints() if web.leaf_id != observer.leaf_id
    )
    return victim.ip


def lpm_prefix_for_dst(intent: dict[str, Any], switch: str, dst_ip: str) -> str | None:
    """Return the longest programmed LPM prefix on ``switch`` that covers ``dst_ip``."""
    addr = ipaddress.ip_address(dst_ip)
    entries = intent["switches"][switch].get("ipv4_lpm") or []
    best: str | None = None
    best_len = -1
    for entry in entries:
        network = ipaddress.ip_network(str(entry["prefix"]), strict=False)
        if addr in network and int(network.prefixlen) > best_len:
            best = str(entry["prefix"])
            best_len = int(network.prefixlen)
    return best


def wrong_local_group_id(
    intent: dict[str, Any], switch: str, prefix: str
) -> int | None:
    """Pick a local-host ActionSelector group that misroutes traffic off the rack."""
    entries = intent["switches"][switch].get("ipv4_lpm") or []
    for entry in entries:
        if str(entry["prefix"]) != prefix and entry.get("kind") == "local_host":
            return int(entry["group_id"])
    return other_group_id(intent, switch, prefix)


def fabric_table_entry_prefix(
    intent: dict[str, Any], switch: str, scenario_name: str | None
) -> str | None:
    """Resolve the LPM prefix for table-entry faults on supported P4 fabrics."""
    if scenario_name == "p4_dc_fabric":
        dst_ip = probe_victim_ip(intent)
        return lpm_prefix_for_dst(intent, switch, dst_ip)
    return lpm_prefix(intent, switch)


def fabric_misconfig_group_id(
    intent: dict[str, Any], switch: str, prefix: str, scenario_name: str | None
) -> int | None:
    """Resolve a wrong ActionSelector group for table-entry misconfig faults."""
    if scenario_name == "p4_dc_fabric":
        return wrong_local_group_id(intent, switch, prefix)
    return other_group_id(intent, switch, prefix)


def gateway_backend_leaf(model: Any) -> str:
    """Return the leaf switch hosting the default VIP backend pool."""
    backend = model.backend_pool[0]
    return str(backend.attached_switch)


def ecmp_target(intent: dict[str, Any], switch: str) -> tuple[str, int, int, str]:
    """Return an LPM entry and one member from a multi-member selector group."""
    state = intent["switches"][switch]
    groups = state.get("groups") or []
    group = next(
        (item for item in groups if len(item.get("member_ids") or []) > 1), None
    )
    if group is None:
        raise RuntimeError(f"No ECMP group on {switch}")
    group_id = int(group["group_id"])
    prefix = next(
        str(entry["prefix"])
        for entry in state.get("ipv4_lpm") or []
        if int(entry["group_id"]) == group_id
    )
    member_id = int(group["member_ids"][0])
    peer = next(
        str(member["peer"])
        for member in state.get("members") or []
        if int(member["member_id"]) == member_id
    )
    return prefix, group_id, member_id, peer


_STAGED_PIPELINE_STEM = "pipeline_candidate"


def lpm_capacity(intent: dict[str, Any]) -> int:
    """Return the pipeline LPM capacity exposed by the installed intent."""
    return int((intent.get("pipeline") or {}).get("ipv4_lpm_size") or 256)


def load_blackhole_pipeline(
    runtime: LabRuntime, switch: str, source: Path
) -> tuple[str, str]:
    """Compile a drop-all pipeline and stage it for the shared manager.

    Staging files use a neutral name and the switch-side build files are removed,
    so the lab filesystem does not name the fault. Call
    :func:`remove_staged_pipeline` after pushing the pipeline.
    """
    from nika.net_env.p4_dc_fabric.fabric_manager.apply import (
        FABRIC_DIR,
        _copy_in,
        _copy_out,
        compile_pipeline_on_switch,
    )

    stem = _STAGED_PIPELINE_STEM
    p4info = f"{FABRIC_DIR}/{stem}.p4info.txt"
    json_path = f"{FABRIC_DIR}/{stem}.json"
    _copy_in(runtime, switch, f"/tmp/{stem}.p4", source.read_bytes())
    compile_pipeline_on_switch(
        runtime, switch, f"/tmp/{stem}.p4", f"{stem}.p4info.txt", f"{stem}.json"
    )
    _copy_in(
        runtime,
        "fabric_mgr",
        json_path,
        _copy_out(runtime, switch, f"/tmp/{stem}.json"),
    )
    _copy_in(
        runtime,
        "fabric_mgr",
        p4info,
        _copy_out(runtime, switch, f"/tmp/{stem}.p4info.txt"),
    )
    runtime.exec(
        switch,
        f"rm -f /tmp/{stem}.p4 /tmp/{stem}.p4i /tmp/{stem}.json /tmp/{stem}.p4info.txt",
    )
    return p4info, json_path


def remove_staged_pipeline(runtime: LabRuntime, p4info: str, json_path: str) -> None:
    """Delete the manager-side staging copies once the pipeline is pushed."""
    runtime.exec("fabric_mgr", f"rm -f {p4info} {json_path}")


def capture_entity(runtime, switch, kind, field, value):
    """Capture one forwarding entity before its first mutation."""
    import copy

    result = run_manager(runtime, "read", "--switch", switch, timeout=60)
    state = (result.get("switches") or {}).get(switch) or {}
    row = next((r for r in state.get(kind, []) if r.get(field) == value), None)
    if row is None:
        raise RuntimeError(f"Cannot capture {kind} {value} on {switch}")
    return copy.deepcopy(row)


def restore_entities(problem, switch, entities):
    """Restore captured forwarding entities and compare their live fields."""
    from nika.problems.base import build_verify_result
    from nika.problems.support.p4runtime_writes import p4runtime_write

    if not entities:
        return build_verify_result(
            fault_type=problem.root_cause_name,
            verified=False,
            details={"host": switch, "reason": "No forwarding snapshot was captured."},
        )

    kinds = {
        "member": ("members", "member_id"),
        "group": ("groups", "group_id"),
        "lpm": ("ipv4_lpm", "prefix"),
    }
    result = run_manager(problem.runtime, "read", "--switch", switch, timeout=60)
    state = (result.get("switches") or {}).get(switch) or {}
    updates = []
    for kind, original in entities:
        collection, key = kinds[kind]
        present = any(
            row.get(key) == original[key] for row in state.get(collection, [])
        )
        updates.append({"type": "MODIFY" if present else "INSERT", kind: original})
    written = p4runtime_write(problem.runtime, switch, updates) if updates else {}
    observed = run_manager(problem.runtime, "read", "--switch", switch, timeout=60)
    live = (observed.get("switches") or {}).get(switch) or {}
    ok = bool(entities) and bool(written.get("ok"))
    for kind, original in entities:
        collection, key = kinds[kind]
        row = next(
            (r for r in live.get(collection, []) if r.get(key) == original[key]), {}
        )
        ok = ok and all(row.get(field) == value for field, value in original.items())
    return build_verify_result(
        fault_type=problem.root_cause_name,
        verified=ok,
        details={"host": switch, "write": written},
    )


def capture_switch(runtime, switch):
    """Capture the live switch state before a destructive dataplane failure."""
    import copy

    state = (
        run_manager(runtime, "read", "--switch", switch, timeout=60).get("switches")
        or {}
    ).get(switch)
    if not state or not state.get("pipeline"):
        raise RuntimeError(f"Cannot capture P4 switch {switch}")
    return copy.deepcopy(state)


def restore_switch(problem, switch, original):
    """Rebuild captured forwarding state with the installed pipeline and intent."""
    import copy
    import json

    from nika.net_env.p4_dc_fabric.fabric_manager.apply import INTENT_PATH, _copy_in
    from nika.problems.base import build_verify_result
    from nika.problems.support.p4runtime_writes import p4runtime_write, require_ok

    if original is None:
        return build_verify_result(
            fault_type=problem.root_cause_name,
            verified=False,
            details={"host": switch, "reason": "No switch snapshot was captured."},
        )
    fields = ("members", "groups", "ipv4_lpm", "pipeline", "runtime_config")
    try:
        current = capture_switch(problem.runtime, switch)
    except RuntimeError:
        current = None
    if current is not None and all(
        current.get(key) == original.get(key) for key in fields
    ):
        return build_verify_result(
            fault_type=problem.root_cause_name, verified=True, details={"host": switch}
        )
    intent = load_intent(problem.runtime)
    replay = copy.deepcopy(intent)
    for kind in ("members", "groups", "ipv4_lpm"):
        replay["switches"][switch][kind] = original[kind]
    try:
        _copy_in(
            problem.runtime, "fabric_mgr", INTENT_PATH, json.dumps(replay).encode()
        )
        require_ok(
            run_manager(problem.runtime, "apply", "--switch", switch, timeout=180),
            "P4 switch replay",
        )
    finally:
        _copy_in(
            problem.runtime,
            "fabric_mgr",
            INTENT_PATH,
            json.dumps(intent, indent=2).encode(),
        )
    observed = capture_switch(problem.runtime, switch)
    updates = []
    schemas = {
        "ecn_config": ("egress_port", "set_ecn_threshold", "threshold"),
        "int_mtu_config": ("egress_spec", "set_int_mtu", "mtu"),
        "egress_mtu": ("egress_spec", "set_egress_mtu", "mtu"),
    }
    for table, (field, action, param) in schemas.items():
        for row in (original.get("runtime_config") or {}).get(table, []):
            if not row.get("is_default"):
                updates.append(
                    {
                        "type": "MODIFY",
                        "table": table,
                        "match": {field: row["key"]},
                        "action": action,
                        "params": {param: row["param_1"]},
                    }
                )
    old_acl = (original.get("runtime_config") or {}).get("icmp_acl", [])
    live_acl = (observed.get("runtime_config") or {}).get("icmp_acl", [])
    old_keys = {tuple(row["keys"]) for row in old_acl if not row.get("is_default")}
    live_keys = {tuple(row["keys"]) for row in live_acl if not row.get("is_default")}
    for key in live_keys - old_keys:
        updates.append(
            {
                "type": "DELETE",
                "table": "icmp_acl",
                "match": {"type": key[0], "code": key[1]},
            }
        )
    for key in old_keys - live_keys:
        updates.append(
            {
                "table": "icmp_acl",
                "match": {"type": key[0], "code": key[1]},
                "action": "drop",
            }
        )
    if updates:
        require_ok(
            p4runtime_write(problem.runtime, switch, updates),
            "P4 runtime config replay",
        )
    observed = capture_switch(problem.runtime, switch)
    ok = all(observed.get(kind) == original.get(kind) for kind in fields)
    return build_verify_result(
        fault_type=problem.root_cause_name, verified=ok, details={"host": switch}
    )


# TEST-NET-3 is outside the authored and filler address pools.
TABLE_PROBE_PREFIX = "203.0.113.77/32"
TABLE_FULL_MESSAGE = "Error when adding match entry to target"
TABLE_CAPACITY_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[5])
import p4rt_manager as m
name, prefix, intent_path, p4info_path = sys.argv[1:5]
switch = m.load_json(intent_path)["switches"][name]
index = m.P4InfoIndex(m.load_p4info(p4info_path))
entry = {"prefix": prefix, "group_id": int(switch["ipv4_lpm"][0]["group_id"])}
client = m.connect(switch)
out = {"prefix": prefix, "inserted": False, "codes": [], "messages": []}
try:
    before = m.observed_switch(client, index)["ipv4_lpm"]
    out["occupancy"] = len(before)
    if any(row["prefix"] == prefix for row in before):
        raise RuntimeError("Diagnostic prefix already exists")
    try:
        statuses = m.write_updates(client, [m.lpm_entity(index, entry)])
        out["codes"] = [status["code"] for status in statuses]
        out["messages"] = [status["message"] for status in statuses]
        out["inserted"] = out["codes"] == ["OK"]
    finally:
        live = m.observed_switch(client, index)["ipv4_lpm"]
        if any(row["prefix"] == prefix for row in live):
            cleanup = m.write_updates(client, [m.lpm_entity(index, entry, kind="DELETE")])
            if any(status["code"] != "OK" for status in cleanup):
                raise RuntimeError("Diagnostic route cleanup failed")
        after = m.observed_switch(client, index)["ipv4_lpm"]
        out["present_after"] = any(row["prefix"] == prefix for row in after)
except Exception as exc:
    out["error"] = str(exc)
finally:
    client.close()
print(json.dumps(out))
"""


def table_capacity_command(switch: str) -> str:
    """Build a diagnostic that attempts one route and removes it if installed."""
    from nika.net_env.p4_dc_fabric.fabric_manager.apply import (
        INTENT_PATH,
        P4INFO_PATH,
        MANAGER_DIR,
    )

    return (
        f"python3 -B -c {shlex.quote(TABLE_CAPACITY_PROBE)} {shlex.quote(switch)} "
        f"{TABLE_PROBE_PREFIX} {INTENT_PATH} {P4INFO_PATH} {MANAGER_DIR}"
    )


def probe_table_capacity(runtime: LabRuntime, switch: str) -> dict[str, Any]:
    """Try an unused route; report device status and verify diagnostic cleanup."""
    try:
        output = runtime.exec("fabric_mgr", table_capacity_command(switch), timeout=60)
        return json.loads(output.strip().splitlines()[-1])
    except (RuntimeError, ValueError, IndexError) as exc:
        return {"error": str(exc)}


def table_capacity_rejected(result: dict[str, Any]) -> bool:
    """Accept resource errors, including BMv2's specific target-full response."""
    codes = set(result.get("codes") or [])
    messages = result.get("messages") or []
    return (
        "error" not in result
        and result.get("inserted") is False
        and result.get("present_after") is False
        and (
            codes == {"RESOURCE_EXHAUSTED"}
            or codes == {"UNKNOWN"}
            and TABLE_FULL_MESSAGE in messages
        )
    )
