#!/usr/bin/env python
"""
Skill-side discovery helper for hidden topology and L2 inventory checks.

This keeps discovery logic out of the Kathara MCP server surface while still
letting the agent enumerate devices, links, and per-device L2 state when the
task description is only a partial sample of the lab.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


SRC_ROOT = Path(__file__).resolve().parents[7]
REPO_ROOT = SRC_ROOT.parent
DEFAULT_MAC_GROUPS = ("hosts", "routers", "switches", "servers", "other_devices")
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def _load_api_class():
    try:
        from nika.service.kathara import KatharaAPIALL as KatharaAPI  # noqa: E402
    except ModuleNotFoundError as exc:
        venv_python = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
        raise SystemExit(
            f"Missing project dependency '{exc.name}'. "
            f"Run this script with the project interpreter, e.g. '{venv_python}'."
        ) from exc
    return KatharaAPI


def _safe_container_name(container: Any) -> str | None:
    labels = getattr(container, "labels", None) or {}
    return labels.get("name") or getattr(container, "name", None)


def _safe_link_map(api: Any) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    try:
        links: dict[str, Any] = next(api.instance.get_links_stats())
    except Exception as exc:  # pragma: no cover - depends on live lab state
        return {}, [{"type": "get_links_failed", "message": str(exc)}]

    result: dict[str, list[str]] = {}
    warnings: list[dict[str, Any]] = []
    for raw_name, link in links.items():
        link_name = getattr(link, "name", None) or raw_name or "<unnamed-link>"
        containers = getattr(link, "containers", None) or []
        names = [name for container in containers if (name := _safe_container_name(container))]

        if len(names) >= 2:
            result[link_name] = sorted(set(names))
            continue

        warnings.append(
            {
                "type": "incomplete_link",
                "link": link_name,
                "container_count": len(containers),
                "resolved_devices": names,
            }
        )

    return result, warnings


def _neighbors_from_link_map(link_map: dict[str, list[str]], host_name: str) -> list[str]:
    neighbors: set[str] = set()
    for devices in link_map.values():
        if host_name not in devices:
            continue
        for device in devices:
            if device != host_name:
                neighbors.add(device)
    return sorted(neighbors)


def _parse_l2_inventory(api: Any, host_name: str, include_loopback: bool, include_bridges: bool) -> list[dict]:
    result = api.exec_cmd(host_name, "ip -j addr")
    output = "\n".join(result) if isinstance(result, list) else result
    if not output:
        return []

    links = json.loads(output)
    inventory = []
    for link in links:
        name = link.get("ifname")
        if not name:
            continue
        if not include_loopback and name == "lo":
            continue
        if not include_bridges and "br" in name:
            continue

        ipv4 = []
        for addr in link.get("addr_info", []):
            if addr.get("family") != "inet":
                continue
            ip = addr.get("local")
            prefix = addr.get("prefixlen")
            if ip and not ip.startswith("127."):
                ipv4.append(f"{ip}/{prefix}" if prefix is not None else ip)

        inventory.append(
            {
                "ifname": name,
                "mac": link.get("address"),
                "operstate": link.get("operstate"),
                "flags": link.get("flags", []),
                "master": link.get("master"),
                "ipv4": ipv4,
            }
        )

    return inventory


def _flatten_server_groups(server_groups: dict) -> list[str]:
    devices = []
    for members in server_groups.values():
        devices.extend(members)
    return sorted(set(devices))


def _lab_groups(api: Any) -> dict[str, list[str] | dict]:
    api.load_machines()
    groups = {
        "hosts": api.hosts,
        "routers": api.routers,
        "switches": api.switches,
        "servers": dict(api.servers),
        "bmv2_switches": api.bmv2_switches,
        "ovs_switches": api.ovs_switches,
        "sdn_controllers": api.sdn_controllers,
    }
    known_devices = set(groups["hosts"])
    known_devices.update(groups["routers"])
    known_devices.update(groups["switches"])
    known_devices.update(_flatten_server_groups(groups["servers"]))
    known_devices.update(groups["bmv2_switches"])
    known_devices.update(groups["ovs_switches"])
    known_devices.update(groups["sdn_controllers"])
    groups["other_devices"] = sorted(set(api.lab.machines.keys()) - known_devices)
    return groups


def _inventory_summary(api: Any) -> dict:
    groups = _lab_groups(api)
    link_map, warnings = _safe_link_map(api)
    return {
        "lab_name": api.lab.name,
        **groups,
        "links": link_map,
        "warnings": warnings,
    }


def _preview(items: list[str], limit: int = 12) -> str:
    if not items:
        return "(none)"
    preview = ", ".join(items[:limit])
    if len(items) > limit:
        preview += f" ... (+{len(items) - limit} more)"
    return preview


def _summary_text(payload: dict[str, Any]) -> str:
    lines = []
    lines.append("=== NETWORK INVENTORY ===")
    lines.append(f"Lab: {payload['lab_name']}")
    lines.append(f"Hosts: {len(payload['hosts'])} [{_preview(payload['hosts'])}]")
    lines.append(f"Routers: {len(payload['routers'])} [{_preview(payload['routers'])}]")
    lines.append(f"Switches: {len(payload['switches'])} [{_preview(payload['switches'])}]")

    server_total = sum(len(members) for members in payload["servers"].values())
    if payload["servers"]:
        lines.append(f"Servers: {server_total}")
        for role, members in sorted(payload["servers"].items()):
            lines.append(f"  {role}: {len(members)} [{_preview(sorted(members))}]")
    else:
        lines.append("Servers: 0")

    if payload["bmv2_switches"]:
        lines.append(f"BMv2 switches: {len(payload['bmv2_switches'])} [{_preview(payload['bmv2_switches'])}]")
    if payload["ovs_switches"]:
        lines.append(f"OVS switches: {len(payload['ovs_switches'])} [{_preview(payload['ovs_switches'])}]")
    if payload["sdn_controllers"]:
        lines.append(
            f"SDN controllers: {len(payload['sdn_controllers'])} [{_preview(payload['sdn_controllers'])}]"
        )
    if payload["other_devices"]:
        lines.append(f"Other devices: {len(payload['other_devices'])} [{_preview(payload['other_devices'])}]")

    lines.append(f"Links discovered: {len(payload['links'])}")
    for link_name, devices in sorted(payload["links"].items()):
        lines.append(f"  {link_name}: {', '.join(devices)}")

    if payload["warnings"]:
        lines.append("Warnings:")
        for warning in payload["warnings"]:
            lines.append(f"  {warning}")
    return "\n".join(lines) + "\n"


def _connected_text(payload: dict[str, Any]) -> str:
    lines = []
    lines.append("=== DIRECT NEIGHBORS ===")
    lines.append(f"Device: {payload['device']}")
    lines.append(f"Neighbors: {_preview(payload['neighbors'], limit=len(payload['neighbors']) or 1)}")
    if payload["warnings"]:
        lines.append("Warnings:")
        for warning in payload["warnings"]:
            lines.append(f"  {warning}")
    return "\n".join(lines) + "\n"


def _l2_text(payload: dict[str, Any]) -> str:
    lines = []
    lines.append("=== L2 INVENTORY ===")
    lines.append(f"Device: {payload['device']}")
    if payload.get("error"):
        lines.append(f"Error: {payload['error']}")
        return "\n".join(lines) + "\n"

    if not payload["interfaces"]:
        lines.append("Interfaces: none")
        return "\n".join(lines) + "\n"

    for interface in payload["interfaces"]:
        ipv4_text = ", ".join(interface["ipv4"]) if interface["ipv4"] else "(none)"
        flags_text = ", ".join(interface["flags"]) if interface["flags"] else "(none)"
        lines.append(interface["ifname"])
        lines.append(f"  MAC: {interface['mac'] or '(none)'}")
        lines.append(f"  Operstate: {interface['operstate'] or '(unknown)'}")
        lines.append(f"  IPv4: {ipv4_text}")
        lines.append(f"  Flags: {flags_text}")
        if interface.get("master"):
            lines.append(f"  Master: {interface['master']}")
    return "\n".join(lines) + "\n"


def _macs_text(payload: dict[str, Any]) -> str:
    lines = []
    lines.append("=== MAC INVENTORY ===")
    if payload["groups_requested"]:
        lines.append(f"Scope: {', '.join(payload['groups_requested'])}")
    lines.append(f"Devices scanned: {payload['device_count']}")
    lines.append(f"Devices: {_preview(payload['devices_scanned'], limit=len(payload['devices_scanned']) or 1)}")
    lines.append(f"Interfaces scanned: {payload['interface_count']}")
    lines.append(f"Duplicate MAC groups: {payload['duplicate_count']}")
    if payload["neighbor_expansion"]:
        lines.append("Neighbor expansion: enabled")
    if payload["device_errors"]:
        lines.append("Device errors:")
        for item in payload["device_errors"]:
            lines.append(f"  {item['device']}: {item['error']}")

    if payload["duplicates"]:
        lines.append("Duplicate MACs:")
        for item in payload["duplicates"]:
            lines.append(f"  {item['mac']}: {', '.join(item['devices'])}")
    else:
        lines.append("Duplicate MACs: none")
    return "\n".join(lines) + "\n"


def _resolve_devices(
    api: Any,
    devices: list[str],
    groups: list[str],
    *,
    expand_neighbors: bool,
) -> list[str]:
    inventory = _lab_groups(api)
    resolved = list(devices)
    for group in groups:
        if group == "all":
            resolved.extend(inventory["hosts"])
            resolved.extend(inventory["routers"])
            resolved.extend(inventory["switches"])
            resolved.extend(_flatten_server_groups(inventory["servers"]))
            resolved.extend(inventory["bmv2_switches"])
            resolved.extend(inventory["ovs_switches"])
            resolved.extend(inventory["sdn_controllers"])
            resolved.extend(inventory["other_devices"])
            continue
        if group == "servers":
            resolved.extend(_flatten_server_groups(inventory["servers"]))
            continue
        if group == "other_devices":
            resolved.extend(inventory["other_devices"])
            continue
        resolved.extend(inventory[group])

    if expand_neighbors and resolved:
        link_map, _ = _safe_link_map(api)
        pending = list(resolved)
        seen = set(resolved)
        while pending:
            device = pending.pop()
            for neighbor in _neighbors_from_link_map(link_map, device):
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                pending.append(neighbor)
        resolved = list(seen)

    return sorted(set(resolved))


def _find_duplicates(records: Iterable[dict], *, include_records: bool = False) -> list[dict]:
    by_mac: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        mac = record.get("mac")
        if mac:
            by_mac[mac].append(record)

    duplicates = []
    for mac, entries in sorted(by_mac.items()):
        devices = sorted({entry["device"] for entry in entries})
        if len(devices) < 2:
            continue
        duplicates.append(
            {
                "mac": mac,
                "devices": devices,
                "record_count": len(entries),
                **({"records": entries} if include_records else {}),
            }
        )
    return duplicates


def main() -> int:
    # CLI surface is topology-only. Per-device L2 identity and duplicate-MAC
    # detection are owned by `l2_snapshot.py`; this script exposes only the
    # topology / grouping / neighbor view. The L2 and MAC helper functions
    # remain importable for other scripts.
    parser = argparse.ArgumentParser(description="Discover lab topology and device groupings.")
    parser.set_defaults(command="summary", as_json=False)
    parser.add_argument("--lab", default=os.getenv("LAB_NAME", "campus_lan"))
    subparsers = parser.add_subparsers(dest="command")

    summary_parser = subparsers.add_parser("summary", help="Show grouped devices plus discovered links.")
    summary_parser.add_argument("--json", action="store_true", dest="as_json")

    connected_parser = subparsers.add_parser("connected", help="Show directly connected devices for a node.")
    connected_parser.add_argument("device")
    connected_parser.add_argument("--json", action="store_true", dest="as_json")

    args = parser.parse_args()
    KatharaAPI = _load_api_class()
    api = KatharaAPI(lab_name=args.lab)

    if args.command == "summary":
        payload = _inventory_summary(api)
        if args.as_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(_summary_text(payload), end="")
        return 0

    if args.command == "connected":
        link_map, warnings = _safe_link_map(api)
        payload = {
            "device": args.device,
            "neighbors": _neighbors_from_link_map(link_map, args.device),
            "warnings": warnings,
        }
        if args.as_json:
            print(json.dumps(payload, indent=2))
        else:
            print(_connected_text(payload), end="")
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
