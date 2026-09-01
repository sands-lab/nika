#!/usr/bin/env python
"""
Compact traffic-control coverage helper.

This combines the most common quiet-throughput checks into one pass:
- active interface discovery
- qdisc inspection across the scanned devices
- compact `tc -s` previews only for interfaces with non-default qdisc stacks

Use it when reachability and small HTTP checks look healthy but throughput,
latency variation, or shaping is still plausible.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from typing import Any

from network_inventory import _load_api_class, _parse_l2_inventory, _resolve_devices

DEFAULT_GROUPS = [
    "hosts",
    "routers",
    "switches",
    "servers",
    "bmv2_switches",
    "ovs_switches",
    "sdn_controllers",
    "other_devices",
]
DEFAULT_QDISC_KINDS = {"fq_codel", "noqueue", "mq", "pfifo_fast", "pfifo", "bfifo"}
QDISC_RE = re.compile(
    r"^qdisc\s+(?P<kind>\S+)\s+(?P<handle>\S+):(?:\s+dev\s+(?P<ifname>\S+))?\s+(?P<rest>.+)$"
)
PARENT_RE = re.compile(r"\bparent\s+(?P<parent>\S+)\b")


def _command_failed(output: str) -> bool:
    return output.startswith("[TIMEOUT]") or output.startswith("Machine ") or "not found in lab" in output


def _safe_exec(api: Any, device: str, command: str) -> tuple[str, str | None]:
    output = api.exec_cmd(device, command)
    if isinstance(output, list):
        output = "\n".join(output)
    if _command_failed(output):
        return output, output
    return output, None


def _is_selected_interface(interface: dict[str, Any], include_down: bool) -> bool:
    if interface.get("ifname") == "lo":
        return False
    if include_down:
        return True

    operstate = (interface.get("operstate") or "").upper()
    flags = {flag.upper() for flag in interface.get("flags", [])}
    return operstate in {"UP", "UNKNOWN"} or "UP" in flags


def _parse_qdisc_entries(raw: str) -> dict[str, list[dict[str, str]]]:
    by_interface: dict[str, list[dict[str, str]]] = defaultdict(list)
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped.startswith("qdisc "):
            continue
        match = QDISC_RE.match(stripped)
        if not match:
            continue

        rest = match.group("rest")
        ifname = match.group("ifname")
        if not ifname:
            continue

        role = "root" if " root " in f" {rest} " else "child" if " parent " in f" {rest} " else "other"
        parent_match = PARENT_RE.search(rest)
        by_interface[ifname].append(
            {
                "kind": match.group("kind"),
                "handle": match.group("handle"),
                "ifname": ifname,
                "role": role,
                "parent": parent_match.group("parent") if parent_match else "",
                "raw": stripped,
            }
        )
    return by_interface


def _unique_kinds(entries: list[dict[str, str]]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for entry in entries:
        kind = entry["kind"]
        if kind in seen:
            continue
        seen.add(kind)
        ordered.append(kind)
    return ordered


def _classify_qdisc(entries: list[dict[str, str]]) -> dict[str, Any]:
    if not entries:
        return {
            "flagged": True,
            "classification": "no_qdisc_visible",
            "kind_chain": "(none)",
        }

    root_kinds = [entry["kind"] for entry in entries if entry["role"] == "root"]
    child_kinds = [entry["kind"] for entry in entries if entry["role"] == "child"]
    all_kinds = _unique_kinds(entries)
    kind_set = set(all_kinds)

    if "netem" in root_kinds and "tbf" in child_kinds:
        classification = "incast_traffic_network_limitation"
        flagged = True
    elif "tbf" in root_kinds:
        classification = "link_bandwidth_throttling"
        flagged = True
    elif kind_set and kind_set.issubset(DEFAULT_QDISC_KINDS):
        classification = "default"
        flagged = False
    elif "tbf" in kind_set:
        classification = "tbf_present"
        flagged = True
    elif "netem" in kind_set:
        classification = "netem_present"
        flagged = True
    else:
        classification = "nondefault_tc"
        flagged = True

    return {
        "flagged": flagged,
        "classification": classification,
        "kind_chain": ", ".join(all_kinds) if all_kinds else "(none)",
    }


def _stats_preview(raw: str, max_lines: int = 6) -> list[str]:
    preview: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("qdisc ") or stripped.startswith("Sent ") or stripped.startswith("backlog "):
            preview.append(stripped)
        if len(preview) >= max_lines:
            break
    return preview


def _device_snapshot(
    api: Any,
    device: str,
    *,
    include_down: bool,
    include_bridges: bool,
    stats_for_flagged: bool,
) -> dict[str, Any]:
    try:
        interfaces = _parse_l2_inventory(api, device, include_loopback=False, include_bridges=include_bridges)
    except Exception as exc:  # pragma: no cover - depends on live lab state
        return {
            "device": device,
            "interfaces": [],
            "device_error": str(exc),
        }

    selected_interfaces = [interface for interface in interfaces if _is_selected_interface(interface, include_down)]
    if not selected_interfaces:
        return {
            "device": device,
            "interfaces": [],
            "device_error": None,
        }

    qdisc_raw, qdisc_error = _safe_exec(api, device, "tc qdisc show 2>&1")
    qdisc_by_interface = _parse_qdisc_entries(qdisc_raw) if not qdisc_error else {}
    interface_entries: list[dict[str, Any]] = []

    for interface in selected_interfaces:
        ifname = interface["ifname"]
        entries = qdisc_by_interface.get(ifname, [])
        analysis = _classify_qdisc(entries)
        stats_lines: list[str] = []
        stats_error: str | None = None
        if analysis["flagged"] and stats_for_flagged and not qdisc_error and entries:
            stats_raw, stats_error = _safe_exec(api, device, f"tc -s qdisc show dev {ifname} 2>&1")
            if not stats_error:
                stats_lines = _stats_preview(stats_raw)

        interface_entries.append(
            {
                "ifname": ifname,
                "operstate": interface.get("operstate"),
                "mac": interface.get("mac"),
                "ipv4": interface.get("ipv4", []),
                "qdisc_error": qdisc_error,
                "classification": analysis["classification"],
                "flagged": analysis["flagged"],
                "kind_chain": analysis["kind_chain"],
                "qdisc_lines": [entry["raw"] for entry in entries],
                "stats_lines": stats_lines,
                "stats_error": stats_error,
            }
        )

    return {
        "device": device,
        "interfaces": interface_entries,
        "device_error": qdisc_error,
    }


def _text_summary(payload: dict[str, Any]) -> str:
    lines = []
    lines.append("=== TC SNAPSHOT ===")
    lines.append(f"Lab: {payload['lab_name']}")
    if payload["scope"]:
        lines.append(f"Scope: {', '.join(payload['scope'])}")
    lines.append(f"Devices scanned: {payload['device_count']}")
    lines.append(f"Interfaces inspected: {payload['interface_count']}")
    lines.append(f"Flagged interfaces: {payload['flagged_count']}")
    lines.append(f"Device errors: {len(payload['device_errors'])}")

    if payload["default_profiles"]:
        lines.append("Clean qdisc profiles:")
        for profile, count in payload["default_profiles"]:
            lines.append(f"  - {profile}: {count}")

    if payload["flagged_categories"]:
        lines.append("Flagged categories:")
        for category, count in payload["flagged_categories"]:
            lines.append(f"  - {category}: {count}")

    if payload["device_errors"]:
        lines.append("Device errors:")
        for item in payload["device_errors"]:
            lines.append(f"  - {item['device']}: {item['error']}")

    if payload["flagged_interfaces"]:
        lines.append("Flagged interfaces:")
        for item in payload["flagged_interfaces"]:
            lines.append(
                f"  - {item['device']} {item['ifname']}: "
                f"{item['classification']} | qdiscs={item['kind_chain']}"
            )
            for qdisc_line in item["qdisc_lines"]:
                lines.append(f"    {qdisc_line}")
            for stats_line in item["stats_lines"]:
                lines.append(f"    {stats_line}")
            if item["stats_error"]:
                lines.append(f"    stats_error: {item['stats_error']}")
    elif payload["clean_interfaces"]:
        lines.append("Clean interface profiles:")
        for item in payload["clean_interfaces"]:
            lines.append(f"  - {item['device']} {item['ifname']}: {item['kind_chain']}")
    else:
        lines.append("Flagged interfaces: none")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Compact traffic-control coverage helper.")
    parser.add_argument("--lab", default=os.getenv("LAB_NAME", "campus_lan"))
    parser.add_argument("device", nargs="?", help="Optional single-device shortcut.")
    parser.add_argument("--device", action="append", default=[], dest="devices")
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        choices=[
            "hosts",
            "routers",
            "switches",
            "servers",
            "other_devices",
            "bmv2_switches",
            "ovs_switches",
            "sdn_controllers",
            "all",
        ],
        dest="groups",
    )
    parser.add_argument("--include-bridges", action="store_true")
    parser.add_argument("--include-down", action="store_true")
    parser.add_argument("--show-clean", action="store_true")
    parser.add_argument("--no-stats", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    requested_devices = list(args.devices)
    if args.device:
        requested_devices.append(args.device)
    groups = args.groups if args.groups else ([] if requested_devices else DEFAULT_GROUPS)

    KatharaAPI = _load_api_class()
    api = KatharaAPI(lab_name=args.lab)
    devices = _resolve_devices(api, requested_devices, groups, expand_neighbors=False)
    payload_devices = [
        _device_snapshot(
            api,
            device,
            include_down=args.include_down,
            include_bridges=args.include_bridges,
            stats_for_flagged=not args.no_stats,
        )
        for device in devices
    ]

    flagged_interfaces: list[dict[str, Any]] = []
    clean_interfaces: list[dict[str, Any]] = []
    flagged_categories: Counter[str] = Counter()
    default_profiles: Counter[str] = Counter()
    device_errors: list[dict[str, str]] = []
    interface_count = 0

    for device_entry in payload_devices:
        if device_entry.get("device_error"):
            device_errors.append({"device": device_entry["device"], "error": device_entry["device_error"]})
        for interface_entry in device_entry["interfaces"]:
            interface_count += 1
            item = {"device": device_entry["device"], **interface_entry}
            if interface_entry["flagged"]:
                flagged_interfaces.append(item)
                flagged_categories[interface_entry["classification"]] += 1
            else:
                clean_interfaces.append(item)
                default_profiles[interface_entry["kind_chain"]] += 1

    show_clean = args.show_clean or bool(requested_devices)
    # Drop the per-device raw payload from JSON unless --show-clean is set;
    # on l-size topologies the full `devices` array is the main bloat source.
    payload = {
        "lab_name": api.lab.name,
        "scope": requested_devices if requested_devices else groups,
        "device_count": len(devices),
        "interface_count": interface_count,
        "flagged_count": len(flagged_interfaces),
        "flagged_categories": sorted(flagged_categories.items()),
        "default_profiles": sorted(default_profiles.items()),
        "device_errors": device_errors,
        "flagged_interfaces": flagged_interfaces,
        "clean_interfaces": clean_interfaces if show_clean else [],
        "devices": payload_devices if show_clean else [],
        "devices_omitted": (not show_clean) and bool(payload_devices),
    }

    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(_text_summary(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
