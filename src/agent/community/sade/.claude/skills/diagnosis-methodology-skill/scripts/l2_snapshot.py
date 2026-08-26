#!/usr/bin/env python
"""
Compact Layer 2 coverage helper.

This combines the most common "quiet network" L2 checks into one pass:
- device discovery / neighbor context
- interface state and MACs
- bridge membership visibility
- duplicate-MAC detection across the scanned set

Use this when the agent would otherwise call several one-device L2 checks in a
row just to answer "is there an L2-side anomaly here?".
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

from network_inventory import (
    _find_duplicates,
    _load_api_class,
    _neighbors_from_link_map,
    _parse_l2_inventory,
    _resolve_devices,
    _safe_link_map,
)

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


def _device_flags(interfaces: list[dict[str, Any]]) -> list[str]:
    flags: list[str] = []
    if not interfaces:
        return ["no_non_loopback_interfaces"]

    for interface in interfaces:
        ifname = interface.get("ifname", "<unknown>")
        operstate = (interface.get("operstate") or "").upper()
        if operstate and operstate not in {"UP", "UNKNOWN"}:
            flags.append(f"{ifname}: operstate={operstate}")
    return flags


def _bridge_members(interfaces: list[dict[str, Any]]) -> list[str]:
    members = []
    for interface in interfaces:
        if interface.get("master"):
            members.append(f"{interface['ifname']}->{interface['master']}")
    return members


def _snapshot_payload(
    api: Any,
    devices: list[str],
    *,
    include_loopback: bool,
    include_bridges: bool,
    show_records: bool,
) -> dict[str, Any]:
    link_map, warnings = _safe_link_map(api)
    device_entries: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    device_errors: list[dict[str, str]] = []

    for device in devices:
        try:
            interfaces = _parse_l2_inventory(api, device, include_loopback, include_bridges)
        except Exception as exc:  # pragma: no cover - depends on live lab state
            device_errors.append({"device": device, "error": str(exc)})
            device_entries.append(
                {
                    "device": device,
                    "neighbors": _neighbors_from_link_map(link_map, device),
                    "interface_count": 0,
                    "interfaces": [],
                    "flags": [f"collection_failed: {exc}"],
                }
            )
            continue

        flags = _device_flags(interfaces)
        device_entries.append(
            {
                "device": device,
                "neighbors": _neighbors_from_link_map(link_map, device),
                "interface_count": len(interfaces),
                "interfaces": interfaces,
                "bridge_members": _bridge_members(interfaces),
                "flags": flags,
            }
        )
        for interface in interfaces:
            records.append({"device": device, **interface})

    payload: dict[str, Any] = {
        "lab_name": api.lab.name,
        "devices_scanned": devices,
        "device_count": len(devices),
        "device_errors": device_errors,
        "warnings": warnings,
        "duplicate_macs": _find_duplicates(records),
        "flagged_devices": [
            {"device": entry["device"], "flags": entry["flags"]}
            for entry in device_entries
            if entry["flags"]
        ],
        "devices": device_entries,
    }
    if show_records:
        payload["records"] = records
    return payload


def _text_summary(payload: dict[str, Any], show_clean: bool = False) -> str:
    lines = []
    lines.append("=== L2 SNAPSHOT ===")
    lines.append(f"Lab: {payload['lab_name']}")
    if payload["scope"]:
        lines.append(f"Scope: {', '.join(payload['scope'])}")
    lines.append(f"Devices scanned: {payload['device_count']}")
    lines.append(f"Duplicate MAC groups: {len(payload['duplicate_macs'])}")
    lines.append(f"Flagged devices: {len(payload['flagged_devices'])}")
    bridge_count = sum(1 for entry in payload["devices"] if entry.get("bridge_members"))
    lines.append(f"Devices with bridge members: {bridge_count}")
    lines.append(f"Device errors: {len(payload['device_errors'])}")
    if payload["warnings"]:
        lines.append(f"Link-discovery warnings: {len(payload['warnings'])}")

    if payload["duplicate_macs"]:
        lines.append("")
        lines.append("Duplicate MACs (this is the canonical mac_address_conflict signal):")
        for item in payload["duplicate_macs"]:
            lines.append(f"  - {item['mac']}: {', '.join(item['devices'])}")

    if payload["flagged_devices"]:
        lines.append("")
        lines.append("Flagged devices:")
        for item in payload["flagged_devices"]:
            lines.append(f"  - {item['device']}: {', '.join(item['flags'])}")

    # Only iterate per-device blocks when there are flags or --show-clean.
    # On l-size topologies (180+ devices), the per-device dump is the bloat
    # source and is redundant when nothing is flagged.
    if show_clean:
        lines.append("")
        for entry in payload["devices"]:
            neighbor_text = ", ".join(entry["neighbors"]) if entry["neighbors"] else "(none)"
            lines.append(entry["device"])
            lines.append(f"  Neighbors: {neighbor_text}")
            lines.append(f"  Interfaces: {entry['interface_count']}")
            if entry.get("bridge_members"):
                lines.append(f"  Bridge members: {', '.join(entry['bridge_members'])}")
            lines.append(f"  Flags: {', '.join(entry['flags']) if entry['flags'] else 'none'}")
    elif not payload["duplicate_macs"] and not payload["flagged_devices"]:
        lines.append("")
        lines.append(f"All {payload['device_count']} devices' L2 identity is clean (no duplicate MACs, no down/missing interfaces).")
        lines.append("Use --show-clean to dump per-device interface lists.")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Compact Layer 2 coverage helper.")
    parser.add_argument("--lab", default=os.getenv("LAB_NAME", "campus_lan"))
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
    parser.add_argument("--include-loopback", action="store_true")
    parser.add_argument("--include-bridges", action="store_true")
    parser.add_argument("--show-records", action="store_true")
    parser.add_argument("--show-clean", action="store_true",
        help="Include per-device interface dump in text mode and full devices array in JSON. Off by default to keep l-size output compact.")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--exact-devices",
        action="store_true",
        help="Do not auto-expand explicit device selections to directly connected neighbors.",
    )
    args = parser.parse_args()

    KatharaAPI = _load_api_class()
    api = KatharaAPI(lab_name=args.lab)
    groups = args.groups if args.groups else ([] if args.devices else DEFAULT_GROUPS)
    devices = _resolve_devices(
        api,
        args.devices,
        groups,
        expand_neighbors=(bool(args.devices) and not args.exact_devices),
    )
    payload = _snapshot_payload(
        api,
        devices,
        include_loopback=args.include_loopback,
        include_bridges=args.include_bridges,
        show_records=args.show_records,
    )
    payload["scope"] = args.devices if args.devices else groups
    if args.as_json:
        # Strip the per-device interface dump from JSON unless --show-clean.
        # On l-size topologies (180+ devices x 2-3 interfaces), the full
        # `devices` array is the bloat source and would trigger the oversized-
        # output detour. Keep flagged_devices, duplicate_macs, and counts.
        if args.show_clean:
            print(json.dumps(payload, indent=2))
        else:
            json_payload = dict(payload)
            json_payload["devices"] = []
            json_payload["devices_omitted"] = bool(payload.get("devices"))
            print(json.dumps(json_payload, indent=2))
    else:
        print(_text_summary(payload, show_clean=args.show_clean), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
