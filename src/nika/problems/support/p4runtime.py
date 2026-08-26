"""Scenario-neutral helpers for P4Runtime forwarding failures."""

from __future__ import annotations

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


def lpm_capacity(intent: dict[str, Any]) -> int:
    """Return the pipeline LPM capacity exposed by the installed intent."""
    return int((intent.get("pipeline") or {}).get("ipv4_lpm_size") or 256)


def load_blackhole_pipeline(
    runtime: LabRuntime, switch: str, source: Path
) -> tuple[str, str]:
    """Compile a drop-all pipeline and stage it for the shared manager."""
    from nika.net_env.p4_dc_fabric.fabric_manager.apply import (
        FABRIC_DIR,
        _copy_in,
        _copy_out,
        compile_pipeline_on_switch,
    )

    p4info = f"{FABRIC_DIR}/blackhole.p4info.txt"
    json_path = f"{FABRIC_DIR}/blackhole.json"
    _copy_in(runtime, switch, "/tmp/blackhole.p4", source.read_bytes())
    compile_pipeline_on_switch(
        runtime, switch, "/tmp/blackhole.p4", "blackhole.p4info.txt", "blackhole.json"
    )
    _copy_in(
        runtime,
        "fabric_mgr",
        json_path,
        _copy_out(runtime, switch, "/tmp/blackhole.json"),
    )
    _copy_in(
        runtime,
        "fabric_mgr",
        p4info,
        _copy_out(runtime, switch, "/tmp/blackhole.p4info.txt"),
    )
    return p4info, json_path
