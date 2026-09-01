#!/usr/bin/env python
"""
Route-selection drill-down for one or more hosts.

Use this ONLY when you already have a suspicious host and a specific
destination, and you need to see exactly which interface / next-hop /
source IP the kernel picks. Broader host L3 state (interfaces, routes,
resolver, ARP) is owned by `infra_sweep --device <host>`, which covers
every device with one compound exec — this helper does not duplicate it.

Unique output:
- `ip route get <target>` result per scanned host
- matching neighbor state (ARP/ND) for the next-hop the kernel chose
- a one-line L3 context summary (IPv4 present / default route present /
  resolver count) so the route-get answer can be read without flipping to
  another helper

If no target is supplied the helper still runs and prints the one-line L3
context, but the main deliverable (`ip route get`) is skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any

from network_inventory import _load_api_class, _parse_l2_inventory, _resolve_devices


# Route-get lines look like:
#   10.200.0.2 via 10.1.1.1 dev eth0 src 10.1.1.10 uid 0
# We extract the next-hop "via" and the egress "dev" so the neighbor state
# for the next-hop can be looked up without re-parsing elsewhere.
ROUTE_GET_VIA_RE = re.compile(r"\bvia\s+(\S+)")
ROUTE_GET_DEV_RE = re.compile(r"\bdev\s+(\S+)")
ROUTE_GET_SRC_RE = re.compile(r"\bsrc\s+(\S+)")


def _command_failed(output: str) -> bool:
    return output.startswith("[TIMEOUT]") or output.startswith("Machine ") or "not found in lab" in output


def _safe_exec(api: Any, device: str, command: str) -> tuple[str, str | None]:
    output = api.exec_cmd(device, command)
    if _command_failed(output):
        return output, output
    return output, None


def _pick_target(api: Any, target_ip: str | None, target_device: str | None) -> str | None:
    if target_ip:
        return target_ip
    if target_device:
        try:
            return api.get_host_ip(target_device)
        except Exception:  # pragma: no cover - depends on live lab state
            return None
    return None


def _parse_route_get(raw: str) -> dict[str, str]:
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("RTNETLINK") or stripped.startswith("cache"):
            continue
        record: dict[str, str] = {"raw": stripped}
        via = ROUTE_GET_VIA_RE.search(stripped)
        dev = ROUTE_GET_DEV_RE.search(stripped)
        src = ROUTE_GET_SRC_RE.search(stripped)
        if via:
            record["via"] = via.group(1)
        if dev:
            record["dev"] = dev.group(1)
        if src:
            record["src"] = src.group(1)
        return record
    return {}


def _neighbor_state_for(api: Any, device: str, next_hop: str) -> dict[str, str] | None:
    raw, error = _safe_exec(api, device, f"ip neigh show to {next_hop}")
    if error or not raw.strip():
        return None
    parts = raw.strip().split()
    if not parts:
        return None
    entry = {"raw": raw.strip()}
    if "lladdr" in parts:
        try:
            entry["lladdr"] = parts[parts.index("lladdr") + 1]
        except (ValueError, IndexError):
            pass
    if parts[-1].isupper() and parts[-1].replace("_", "").isalpha():
        entry["state"] = parts[-1]
    return entry


def _l3_context(api: Any, device: str) -> dict[str, Any]:
    """Compact one-line-per-host L3 context — NOT a full state dump."""
    try:
        interfaces = _parse_l2_inventory(api, device, include_loopback=False, include_bridges=False)
    except Exception as exc:  # pragma: no cover - depends on live lab state
        return {"error": str(exc), "ipv4_present": None, "default_present": None, "resolver_count": None}

    ipv4_present = any(interface.get("ipv4") for interface in interfaces)
    route_raw, route_error = _safe_exec(api, device, "ip route")
    default_present = bool(
        route_raw and any(line.strip().startswith("default ") for line in route_raw.splitlines())
    ) if not route_error else None
    resolv_raw, resolv_error = _safe_exec(api, device, "cat /etc/resolv.conf 2>/dev/null")
    resolver_count = None
    if not resolv_error:
        resolver_count = sum(
            1 for line in resolv_raw.splitlines() if line.strip().startswith("nameserver ")
        )
    return {
        "ipv4_present": ipv4_present,
        "default_present": default_present,
        "resolver_count": resolver_count,
    }


def _device_snapshot(api: Any, device: str, target: str | None) -> dict[str, Any]:
    context = _l3_context(api, device)
    route_get: dict[str, Any] = {}
    neighbor: dict[str, str] | None = None

    if target:
        raw, error = _safe_exec(api, device, f"ip route get {target}")
        if error:
            route_get = {"error": error}
        else:
            parsed = _parse_route_get(raw)
            route_get = {"raw": raw.strip(), **parsed}
            if parsed.get("via"):
                neighbor = _neighbor_state_for(api, device, parsed["via"])

    flags: list[str] = []
    if context.get("ipv4_present") is False:
        flags.append("no_ipv4_address")
    if context.get("default_present") is False:
        flags.append("no_default_route")
    if target and route_get.get("error"):
        flags.append("route_get_failed")
    if neighbor and neighbor.get("state") in {"FAILED", "INCOMPLETE"}:
        flags.append(f"next_hop_state_{neighbor['state'].lower()}")

    return {
        "device": device,
        "context": context,
        "target": target,
        "route_get": route_get,
        "next_hop_neighbor": neighbor,
        "flags": flags,
    }


def _text_summary(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("=== HOST PATH SNAPSHOT ===")
    lines.append(f"Lab: {payload['lab_name']}")
    lines.append(f"Target: {payload['target'] or '(none — route-get skipped; run infra_sweep for L3 state)'}")
    lines.append("")
    for item in payload["devices"]:
        ctx = item["context"]
        ipv4 = "yes" if ctx.get("ipv4_present") else ("no" if ctx.get("ipv4_present") is False else "?")
        default = "yes" if ctx.get("default_present") else ("no" if ctx.get("default_present") is False else "?")
        resolver_count = ctx.get("resolver_count")
        resolvers = str(resolver_count) if resolver_count is not None else "?"
        lines.append(item["device"])
        lines.append(f"  L3 context: ipv4={ipv4} default_route={default} resolvers={resolvers}")
        if item["target"]:
            rg = item["route_get"]
            if rg.get("error"):
                lines.append(f"  Route to {item['target']}: ERROR — {rg['error']}")
            elif rg:
                via = rg.get("via", "(direct)")
                dev = rg.get("dev", "?")
                src = rg.get("src", "?")
                lines.append(f"  Route to {item['target']}: via={via} dev={dev} src={src}")
                lines.append(f"    raw: {rg.get('raw', '')}")
            if item["next_hop_neighbor"]:
                n = item["next_hop_neighbor"]
                lines.append(
                    f"  Next-hop neighbor: lladdr={n.get('lladdr', '(none)')} state={n.get('state', '?')}"
                )
        lines.append(f"  Flags: {', '.join(item['flags']) if item['flags'] else 'none'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Route-selection drill-down (ip route get) for one or more hosts."
    )
    parser.add_argument("--lab", default=os.getenv("LAB_NAME", "campus_lan"))
    parser.add_argument("device_args", nargs="*", help="Device names to scan.")
    parser.add_argument("--device", action="append", default=[], dest="devices")
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        choices=["hosts", "routers", "switches", "servers", "bmv2_switches", "ovs_switches", "sdn_controllers", "all"],
        dest="groups",
    )
    parser.add_argument("--target-ip")
    parser.add_argument("--target-device")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    if args.device_args:
        if args.devices:
            parser.error("Use positional devices or --device, not both.")
        args.devices = list(args.device_args)

    KatharaAPI = _load_api_class()
    api = KatharaAPI(lab_name=args.lab)
    groups = args.groups if args.groups else ([] if args.devices else ["hosts"])
    devices = _resolve_devices(api, args.devices, groups, expand_neighbors=False)
    target = _pick_target(api, args.target_ip, args.target_device)
    payload = {
        "lab_name": api.lab.name,
        "target": target,
        "devices": [_device_snapshot(api, device, target) for device in devices],
    }
    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(_text_summary(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
