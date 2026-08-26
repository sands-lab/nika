#!/usr/bin/env python
"""
Compact DHCP/link-history helper.

Use this when a host looked suspicious in reachability or an early host sweep,
but current L3 state has already recovered by the time you inspect it.

It combines:
- current link/IP/default-route state
- recent host startup / dhclient log history
- compact pattern extraction for recent DHCP/link trouble

This is especially useful on DHCP topologies where a host can self-heal before
later inspection, leaving only log history behind.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any

from network_inventory import _load_api_class, _resolve_devices


ACK_RE = re.compile(r"DHCPACK of (?P<ip>\S+) from (?P<server>\S+)")
OFFER_RE = re.compile(r"DHCPOFFER of (?P<ip>\S+) from (?P<server>\S+)")
BOUND_RE = re.compile(r"bound to (?P<ip>\S+) -- renewal in (?P<secs>\d+) seconds\.")

NOTABLE_PATTERNS = (
    "No DHCPOFFERS received.",
    "receive_packet failed on eth0: Network is down",
    "send_packet: Network is down",
    "send_packet: Network is unreachable",
    "DHCPOFFER of ",
    "DHCPACK of ",
    "bound to ",
)

HARD_FLAG_PREFIXES = (
    "current_link_down",
    "current_no_default_route",
    "short_valid_lft=",
    "recent_network_down=",
    "recent_network_unreachable=",
    "recent_recovery_after_failure",
    "current_healthy_but_history_dirty",
    "current_down_with_history_dirty",
)


def _command_failed(output: str) -> bool:
    return output.startswith("[TIMEOUT]") or output.startswith("Machine ") or "not found in lab" in output


def _safe_exec(api: Any, device: str, command: str) -> tuple[str, str | None]:
    output = api.exec_cmd(device, command)
    if _command_failed(output):
        return output, output
    return output, None


# Get current link state, IP address, default route presence, and DHCP lease validity from the device.
def _current_link_state(api: Any, device: str) -> dict[str, Any]:
    ip_addr_raw, ip_addr_error = _safe_exec(api, device, "ip addr show dev eth0")
    route_raw, route_error = _safe_exec(api, device, "ip route")

    current_ip = None
    try:
        current_ip = api.get_host_ip(device)
    except Exception:  # pragma: no cover - depends on live lab state
        current_ip = None

    link_state = "unknown"
    if not ip_addr_error:
        if "state DOWN" in ip_addr_raw:
            link_state = "DOWN"
        elif "state UP" in ip_addr_raw:
            link_state = "UP"
        elif "LOWER_UP" in ip_addr_raw:
            link_state = "UP"

    has_default_route = bool(route_raw and any(line.strip().startswith("default ") for line in route_raw.splitlines()))

    valid_lft = None
    match = re.search(r"valid_lft\s+(\d+)sec", ip_addr_raw)
    if match:
        valid_lft = int(match.group(1))
    elif "valid_lft forever" in ip_addr_raw:
        valid_lft = "forever"

    return {
        "link_state": link_state,
        "current_ip": current_ip,
        "has_default_route": has_default_route,
        "valid_lft": valid_lft,
        "ip_addr_error": ip_addr_error,
        "route_error": route_error,
        "ip_addr_raw": ip_addr_raw,
        "route_raw": route_raw,
    }


# Summarize the DHCP/link history from the startup log
def _history_summary(log_raw: str) -> dict[str, Any]:
    lines = [line.rstrip() for line in log_raw.splitlines() if line.strip()]
    no_offers = sum("No DHCPOFFERS received." in line for line in lines)
    network_down = sum("Network is down" in line for line in lines)
    network_unreachable = sum("Network is unreachable" in line for line in lines)
    offers = [OFFER_RE.search(line).groupdict() for line in lines if OFFER_RE.search(line)]
    acks = [ACK_RE.search(line).groupdict() for line in lines if ACK_RE.search(line)]
    bounds = [BOUND_RE.search(line).groupdict() for line in lines if BOUND_RE.search(line)]

    notable_lines = [line for line in lines if any(pattern in line for pattern in NOTABLE_PATTERNS)]
    notable_lines = notable_lines[-8:]

    # `No DHCPOFFERS received` alone is noisy because many hosts show short-lived
    # DHCP churn during startup. Treat it as decisive failure only when there was
    # no later recovery evidence, or when stronger link/path errors also appeared.
    had_failure = network_down > 0 or network_unreachable > 0 or (no_offers > 0 and not (offers or acks or bounds))
    had_recovery = bool(offers or acks or bounds)

    return {
        "no_offers_count": no_offers,
        "network_down_count": network_down,
        "network_unreachable_count": network_unreachable,
        "offer_count": len(offers),
        "ack_count": len(acks),
        "bind_count": len(bounds),
        "had_failure": had_failure,
        "had_recovery": had_recovery,
        "notable_lines": notable_lines,
    }

# broadly summarize the current state and recent history of the device's DHCP/link status, and extract flags for suspicious conditions.
def _device_snapshot(api: Any, device: str, lines: int) -> dict[str, Any]:
    current = _current_link_state(api, device)
    log_raw, log_error = _safe_exec(api, device, f"tail -n {lines} /var/log/startup.log 2>/dev/null")
    history = _history_summary(log_raw) if not log_error else None

    flags: list[str] = []
    if current["link_state"] == "DOWN":
        flags.append("current_link_down")
    if not current["has_default_route"]:
        flags.append("current_no_default_route")
    if isinstance(current["valid_lft"], int) and current["valid_lft"] <= 15:
        flags.append(f"short_valid_lft={current['valid_lft']}s")

    if history:
        noisy_no_offer = history["no_offers_count"] and not (
            history["network_down_count"]
            or history["network_unreachable_count"]
            or current["link_state"] == "DOWN"
            or not current["has_default_route"]
            or (isinstance(current["valid_lft"], int) and current["valid_lft"] <= 15)
        )
        if history["no_offers_count"] and not noisy_no_offer:
            flags.append(f"recent_no_dhcp_offers={history['no_offers_count']}")
        if history["network_down_count"]:
            flags.append(f"recent_network_down={history['network_down_count']}")
        if history["network_unreachable_count"]:
            flags.append(f"recent_network_unreachable={history['network_unreachable_count']}")
        if history["had_failure"] and history["had_recovery"]:
            flags.append("recent_recovery_after_failure")
        if current["link_state"] == "UP" and current["has_default_route"] and history["had_failure"]:
            flags.append("current_healthy_but_history_dirty")
        if current["link_state"] == "DOWN" and history["had_failure"]:
            flags.append("current_down_with_history_dirty")

    return {
        "device": device,
        "current": {
            "link_state": current["link_state"],
            "current_ip": current["current_ip"],
            "has_default_route": current["has_default_route"],
            "valid_lft": current["valid_lft"],
        },
        "history": history,
        "flags": flags,
        # Raw `ip addr` / `ip route` echoes are intentionally NOT re-emitted;
        # those are infra_sweep's job. We keep the startup-log tail because
        # it's this script's unique artifact.
        "startup_log_tail": log_raw,
        "errors": {
            "ip_addr": current["ip_addr_error"],
            "ip_route": current["route_error"],
            "startup_log": log_error,
        },
    }


def _flag_score(item: dict[str, Any]) -> tuple[int, int]:
    flags = item.get("flags", [])
    hard = sum(any(flag.startswith(prefix) for prefix in HARD_FLAG_PREFIXES) for flag in flags)
    return hard, len(flags)


def _text_summary(payload: dict[str, Any]) -> str:
    lines = []
    flagged = [item for item in payload["devices"] if item["flags"]]
    flagged_sorted = sorted(flagged, key=_flag_score, reverse=True)
    flag_counts: dict[str, int] = {}
    for item in flagged:
        for flag in item["flags"]:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1

    lines.append("=== DHCP/LINK HISTORY SNAPSHOT ===")
    lines.append(f"Lab: {payload['lab_name']}")
    lines.append(f"Devices scanned: {len(payload['devices'])}")
    lines.append(f"Devices with suspicious history/state: {len(flagged)}")
    if flag_counts:
        lines.append("Flag counts:")
        for flag, count in sorted(flag_counts.items(), key=lambda pair: (-pair[1], pair[0])):
            lines.append(f"  {flag}: {count}")
    lines.append("")

    devices_to_show = payload["devices"] if payload["include_clean"] else flagged_sorted
    if not devices_to_show:
        lines.append("No suspicious DHCP/link history found in the selected devices.")
        return "\n".join(lines).rstrip() + "\n"

    max_devices = payload["max_devices"] if not payload["include_clean"] else len(devices_to_show)
    shown_devices = devices_to_show[:max_devices]
    omitted = len(devices_to_show) - len(shown_devices)

    for item in shown_devices:
        current = item["current"]
        history = item["history"]
        route_text = "yes" if current["has_default_route"] else "no"
        valid_lft = current["valid_lft"] if current["valid_lft"] is not None else "(unknown)"

        lines.append(item["device"])
        lines.append(
            "  Current: "
            f"link={current['link_state']} ip={current['current_ip'] or '(none)'} "
            f"default_route={route_text} valid_lft={valid_lft}"
        )
        if history:
            lines.append(
                "  History: "
                f"no_offers={history['no_offers_count']} "
                f"network_down={history['network_down_count']} "
                f"network_unreachable={history['network_unreachable_count']} "
                f"offers={history['offer_count']} acks={history['ack_count']} binds={history['bind_count']}"
            )
            if history["notable_lines"]:
                lines.append("  Recent notable lines:")
                for line in history["notable_lines"][-payload["max_notable_lines"] :]:
                    lines.append(f"    {line}")
        lines.append(f"  Flags: {', '.join(item['flags']) if item['flags'] else 'none'}")
        lines.append("")

    if omitted > 0:
        remaining_preview = ", ".join(item["device"] for item in devices_to_show[max_devices : max_devices + 8])
        lines.append(f"... {omitted} more suspicious devices omitted")
        if remaining_preview:
            lines.append(f"Next omitted devices: {remaining_preview}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Compact DHCP/link-history helper.")
    parser.add_argument("--lab", default=os.getenv("LAB_NAME", "campus_lan"))
    parser.add_argument("device_args", nargs="*", help="Optional device names to scan.")
    parser.add_argument("--device", action="append", default=[], dest="devices")
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        choices=["hosts", "routers", "switches", "servers", "bmv2_switches", "ovs_switches", "sdn_controllers", "all"],
        dest="groups",
    )
    parser.add_argument("--lines", type=int, default=80)
    parser.add_argument("--show-clean", action="store_true")
    parser.add_argument("--max-devices", type=int, default=12)
    parser.add_argument("--max-notable-lines", type=int, default=3)
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

    payload = {
        "lab_name": api.lab.name,
        "include_clean": args.show_clean,
        "max_devices": args.max_devices,
        "max_notable_lines": args.max_notable_lines,
        "devices": [_device_snapshot(api, device, args.lines) for device in devices],
    }
    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(_text_summary(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
