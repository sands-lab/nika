#!/usr/bin/env python
"""
Universal infrastructure sweep: one pass across every device.

A single-pass safety net for fault classes that would otherwise need
multiple separate checks:
- nft list ruleset      -> catch ACL/firewall drops (http_acl_block, icmp_acl_block,
                           dns_port_blocked, ospf_acl_block, link_fragmentation_disabled)
- ip -br addr / ip route -> catch missing IPs, missing default routes, wrong netmask,
                           wrong gateway on any host
- arp -n                 -> catch wrong gateway lladdr values (arp_cache_poisoning)
- cat /etc/resolv.conf   -> catch resolver-mismatch (host_incorrect_dns)

The helper is intentionally cheap: it only surfaces flagged devices by default.
Use `--show-clean` to print the unflagged rows too.

The fault-family skills still own the final classification. This script exists
so the diagnosis methodology cannot miss a symptom by accidentally skipping the
ACL or host-config sweep.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from typing import Any

from network_inventory import _load_api_class, _resolve_devices


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

# Drop verdicts that point at a specific fault family. Order matters:
# the first match wins when several keywords appear in the same ruleset line.
ACL_FINGERPRINTS: list[tuple[str, re.Pattern[str], str]] = [
    ("arp_acl_block", re.compile(r"table\s+arp\s+filter", re.IGNORECASE), "arp filter table with drop rules"),
    ("ospf_acl_block", re.compile(r"(\bip\s+protocol\s+ospf\b|\bip\s+protocol\s+89\b|\bproto\s+89\b)", re.IGNORECASE), "OSPF protocol 89 filter"),
    ("bgp_acl_block", re.compile(r"\btcp\s+dport\s+179\b", re.IGNORECASE), "TCP port 179 (BGP) filter"),
    ("http_acl_block", re.compile(r"\btcp\s+dport\s+80\b", re.IGNORECASE), "TCP port 80 filter"),
    ("dns_port_blocked", re.compile(r"\b(tcp|udp)\s+dport\s+53\b", re.IGNORECASE), "port 53 (DNS) filter"),
    ("icmp_acl_block", re.compile(r"\b(icmp\s+type|ip\s+protocol\s+icmp|ip\s+protocol\s+1)\b", re.IGNORECASE), "ICMP filter"),
    ("link_fragmentation_disabled", re.compile(r"(-m\s+length|\bmeta\s+length\b|\bip\s+length\b)", re.IGNORECASE), "packet-length filter"),
]

# Lines that always appear from Docker's default runtime and must not be flagged.
DOCKER_NOISE = re.compile(r"DOCKER_OUTPUT|DOCKER_POSTROUTING|127\.0\.0\.11", re.IGNORECASE)

DROP_RE = re.compile(r"\b(drop|reject)\b", re.IGNORECASE)


def _command_failed(output: str) -> bool:
    return output.startswith("[TIMEOUT]") or output.startswith("Machine ") or "not found in lab" in output


def _safe_exec(api: Any, device: str, command: str) -> tuple[str, str | None]:
    output = api.exec_cmd(device, command)
    if isinstance(output, list):
        output = "\n".join(output)
    if _command_failed(output):
        return output, output
    return output, None


def _parse_nft_rules(raw: str) -> list[dict[str, Any]]:
    """Return one record per rule line that contains drop/reject and is not Docker NAT noise."""
    hits: list[dict[str, Any]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not DROP_RE.search(stripped):
            continue
        if DOCKER_NOISE.search(stripped):
            continue
        classification = "drop_rule"
        reason = "generic drop/reject rule"
        for name, pattern, human in ACL_FINGERPRINTS:
            if pattern.search(stripped):
                classification = name
                reason = human
                break
        hits.append({"rule": stripped, "classification": classification, "reason": reason})
    return hits


def _parse_ip_addr(raw: str) -> list[dict[str, Any]]:
    """Parse `ip -br addr` output. One row per interface with IPv4s."""
    entries = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        ifname, state, *addrs = parts
        if ifname == "lo":
            continue
        ipv4 = [a for a in addrs if "." in a and ":" not in a]
        entries.append({"ifname": ifname, "state": state, "ipv4": ipv4})
    return entries


def _parse_ip_route(raw: str) -> dict[str, Any]:
    defaults = []
    routes = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        routes.append(stripped)
        if stripped.startswith("default"):
            # default via 10.1.1.1 dev eth0
            parts = stripped.split()
            via = parts[parts.index("via") + 1] if "via" in parts else ""
            dev = parts[parts.index("dev") + 1] if "dev" in parts else ""
            defaults.append({"raw": stripped, "via": via, "dev": dev})
    return {"defaults": defaults, "route_count": len(routes)}


MAC_RE = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
# A static (manually-set) ARP entry on a host is the durable, generic
# fingerprint of ARP cache tampering: `arp -s` shows "CM" in `arp -n` output
# and "PERMANENT" in `ip neigh show`. Real OSes do not auto-create static
# entries — anything that sets one did so deliberately, which on an end host
# is almost always an attack or misconfiguration worth surfacing. We do NOT
# blacklist specific "dummy MAC" values (e.g. 00:11:22:33:44:55) because
# that just over-fits to one injector; the static-entry signal catches the
# fault regardless of which MAC the attacker chose.
STATIC_ARP_FLAGS = frozenset({"CM", "PM", "PERMANENT"})


def _looks_fabricated(mac: str) -> str | None:
    """Return a short reason if the MAC looks fabricated, else None.

    Generic heuristics (no specific value blacklist):
    - all zero / all FF / all same byte
    - sequential ascending bytes (00:11:22:33:44:55, 01:02:03:04:05:06, ...)
    These patterns never come out of a real NIC's burned-in address.
    """
    if not MAC_RE.match(mac):
        return None
    try:
        bytes_ = [int(b, 16) for b in mac.split(":")]
    except ValueError:
        return None
    if all(b == 0 for b in bytes_):
        return "all-zero MAC"
    if all(b == 0xFF for b in bytes_):
        return "broadcast MAC in unicast position"
    if len(set(bytes_)) == 1:
        return f"all-same-byte MAC ({mac.split(':')[0]})"
    # Ascending-by-N pattern: e.g. 00:11:22:33:44:55 (delta 0x11)
    deltas = [(bytes_[i + 1] - bytes_[i]) & 0xFF for i in range(5)]
    if len(set(deltas)) == 1 and deltas[0] in (0x01, 0x10, 0x11):
        return f"sequential-byte pattern (delta {deltas[0]:#04x})"
    return None


def _parse_arp(raw: str) -> list[dict[str, str]]:
    """Parse ARP / neighbor table output.

    Supports both `ip neigh show` (preferred, modern) and legacy `arp -n`:
      - `ip neigh show`: "<ip> dev <iface> lladdr <mac> <state>"
      - `arp -n`:        "<ip>  ether  <mac>  <flags> <iface>" (flags optional)
    """
    entries: list[dict[str, str]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("Address"):
            continue
        tokens = stripped.split()
        if not tokens:
            continue

        ip = tokens[0]
        iface = ""
        mac = ""
        flag = ""

        if "lladdr" in tokens:
            # `ip neigh show` format.
            try:
                lladdr_idx = tokens.index("lladdr")
                mac = tokens[lladdr_idx + 1]
            except (ValueError, IndexError):
                mac = ""
            if "dev" in tokens:
                try:
                    dev_idx = tokens.index("dev")
                    iface = tokens[dev_idx + 1]
                except (ValueError, IndexError):
                    iface = ""
            # Last token is usually the state word (REACHABLE / STALE / PERMANENT / ...).
            tail = tokens[-1].upper()
            if tail in {"PERMANENT", "NOARP", "REACHABLE", "STALE", "DELAY", "PROBE", "FAILED", "INCOMPLETE"}:
                flag = tail
        else:
            # Legacy `arp -n` format. Find the MAC by pattern, take iface as
            # last token, and any tokens between MAC and iface as flags.
            mac_positions = [i for i, t in enumerate(tokens) if MAC_RE.match(t)]
            if not mac_positions:
                continue
            mac_idx = mac_positions[0]
            mac = tokens[mac_idx]
            iface = tokens[-1]
            middle = tokens[mac_idx + 1:-1]
            if middle:
                flag = middle[0]
        if not mac:
            continue
        entries.append({"ip": ip, "mac": mac, "iface": iface, "flag": flag})
    return entries


def _parse_nameservers(raw: str) -> list[str]:
    nameservers = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("nameserver "):
            nameservers.append(stripped.split(None, 1)[1])
    return nameservers


# Conservative thresholds: Kathara idle labs should have 0 errors and <=1 carrier
# transition per interface. We only flag sustained signal, not a single reset.
LINK_FLAP_CARRIER_THRESHOLD = 2
LINK_ERROR_THRESHOLD = 5


def _parse_link_stats(raw: str) -> list[dict[str, Any]]:
    """Parse `ip -j -s link show` JSON into compact per-interface records.

    We only keep fields that carry flap / error signal: operstate, carrier
    transitions, and rx/tx error/dropped counters. Non-physical interfaces
    like `lo` are dropped because they do not carry flap semantics.
    """
    try:
        data = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    records: list[dict[str, Any]] = []
    for link in data:
        ifname = link.get("ifname")
        if not ifname or ifname == "lo":
            continue
        stats = link.get("stats64") or {}
        rx = stats.get("rx") or {}
        tx = stats.get("tx") or {}
        carrier = (
            link.get("carrier_changes")
            or link.get("link_info", {}).get("info_slave_data", {}).get("carrier_changes")
            or tx.get("carrier_changes")
            or 0
        )
        try:
            carrier_int = int(carrier)
        except (TypeError, ValueError):
            carrier_int = 0
        records.append(
            {
                "ifname": ifname,
                "operstate": link.get("operstate"),
                "carrier_changes": carrier_int,
                "rx_errors": int(rx.get("errors") or 0),
                "rx_dropped": int(rx.get("dropped") or 0),
                "tx_errors": int(tx.get("errors") or 0),
                "tx_dropped": int(tx.get("dropped") or 0),
            }
        )
    return records


def _link_stats_flags(records: list[dict[str, Any]]) -> list[str]:
    flags: list[str] = []
    for entry in records:
        ifname = entry["ifname"]
        if entry["carrier_changes"] >= LINK_FLAP_CARRIER_THRESHOLD:
            flags.append(
                f"link_flap_suspected: {ifname} carrier_changes={entry['carrier_changes']}"
            )
        if entry["rx_errors"] >= LINK_ERROR_THRESHOLD or entry["tx_errors"] >= LINK_ERROR_THRESHOLD:
            flags.append(
                f"link_errors: {ifname} rx_errors={entry['rx_errors']} tx_errors={entry['tx_errors']}"
            )
        if entry["rx_dropped"] >= LINK_ERROR_THRESHOLD or entry["tx_dropped"] >= LINK_ERROR_THRESHOLD:
            flags.append(
                f"link_drops: {ifname} rx_dropped={entry['rx_dropped']} tx_dropped={entry['tx_dropped']}"
            )
        if entry["operstate"] == "DOWN":
            flags.append(f"interface_down: {ifname}")
    return flags


def _device_sweep(api: Any, device: str, role: str) -> dict[str, Any]:
    # One compound shell call per device, delimited by markers so we do not pay
    # four MCP round-trips for each host. Most devices expose all of these.
    compound = (
        "echo '===NFT==='; nft list ruleset 2>/dev/null || iptables-save 2>/dev/null; "
        "echo '===ADDR==='; ip -br addr; "
        "echo '===ROUTE==='; ip route; "
        "echo '===ARP==='; ip neigh show 2>/dev/null || arp -n; "
        "echo '===RESOLV==='; cat /etc/resolv.conf 2>/dev/null; "
        "echo '===LINKSTATS==='; ip -j -s link show 2>/dev/null"
    )
    raw, error = _safe_exec(api, device, compound)
    if error:
        return {
            "device": device,
            "role": role,
            "error": error,
            "flags": [f"{device}: sweep command failed"],
        }

    sections = {}
    current = None
    for line in raw.splitlines():
        if line.startswith("===") and line.endswith("==="):
            current = line.strip("= ")
            sections[current] = []
            continue
        if current is not None:
            sections[current].append(line)
    for key in ("NFT", "ADDR", "ROUTE", "ARP", "RESOLV", "LINKSTATS"):
        sections.setdefault(key, [])

    nft_hits = _parse_nft_rules("\n".join(sections["NFT"]))
    interfaces = _parse_ip_addr("\n".join(sections["ADDR"]))
    routes = _parse_ip_route("\n".join(sections["ROUTE"]))
    arp_entries = _parse_arp("\n".join(sections["ARP"]))
    nameservers = _parse_nameservers("\n".join(sections["RESOLV"]))
    link_stats = _parse_link_stats("\n".join(sections["LINKSTATS"]))
    link_stat_flags = _link_stats_flags(link_stats)

    ipv4_present = any(iface["ipv4"] for iface in interfaces)
    has_default = bool(routes["defaults"])

    flags: list[str] = []
    for hit in nft_hits:
        flags.append(f"{hit['classification']}: {hit['reason']}")
    flags.extend(link_stat_flags)

    # ARP signals: a static / PERMANENT entry on a host (`arp -s ...`) is the
    # generic fingerprint of cache tampering — real OSes never auto-create
    # those. We additionally flag a MAC that looks fabricated by structural
    # pattern (all-zero, all-same-byte, sequential bytes) — these patterns
    # never come from a real NIC, regardless of which attacker injected them.
    if role == "host":
        for entry in arp_entries:
            if entry.get("flag") in STATIC_ARP_FLAGS:
                flags.append(
                    f"static_arp_entry: {entry['ip']}={entry['mac']} (state={entry['flag']}) — manual `arp -s` suspected"
                )
        for entry in arp_entries:
            reason = _looks_fabricated(entry.get("mac", ""))
            if reason:
                flags.append(
                    f"fabricated_mac_in_arp: {entry['ip']}={entry['mac']} ({reason})"
                )

    if role == "host" and not ipv4_present:
        flags.append("no_ipv4_address")
    if role == "host" and not has_default:
        flags.append("no_default_route")
    # Hosts should have exactly one default route entry most of the time;
    # multiple defaults via different gateways is a spoof/misconfig smell.
    if role == "host" and len({r["via"] for r in routes["defaults"] if r["via"]}) > 1:
        flags.append("multiple_default_gateways")

    return {
        "device": device,
        "role": role,
        "error": None,
        "nft_hits": nft_hits,
        "interfaces": interfaces,
        "default_routes": routes["defaults"],
        "route_count": routes["route_count"],
        "arp": arp_entries,
        "nameservers": nameservers,
        "link_stats": link_stats,
        "flags": flags,
    }


def _role_of(device: str, groups: dict[str, Any]) -> str:
    if device in groups.get("hosts", []):
        return "host"
    if device in groups.get("routers", []):
        return "router"
    if device in groups.get("switches", []):
        return "switch"
    for role, members in (groups.get("servers") or {}).items():
        if device in members:
            return f"server:{role}"
    return "other"


def _cross_host_checks(host_rows: list[dict[str, Any]]) -> list[str]:
    """Add cross-host flags: resolver divergence, ARP gateway lladdr divergence."""
    findings: list[str] = []

    # Resolver groups
    resolver_to_hosts: dict[tuple[str, ...], list[str]] = {}
    for row in host_rows:
        key = tuple(sorted(row["nameservers"]))
        resolver_to_hosts.setdefault(key, []).append(row["device"])
    if len(resolver_to_hosts) > 1:
        desc = "; ".join(
            f"{list(k) or '(empty)'} -> {len(v)} hosts [{', '.join(v[:3])}]"
            for k, v in resolver_to_hosts.items()
        )
        findings.append(f"resolver_divergence: {desc}")

    # Gateway ARP: for each default gateway IP, collect which lladdr each host learned.
    gateway_macs: dict[str, dict[str, list[str]]] = {}
    for row in host_rows:
        gws = {d["via"] for d in row["default_routes"] if d["via"]}
        for gw in gws:
            arp_entry = next((a for a in row["arp"] if a["ip"] == gw), None)
            if arp_entry and arp_entry["mac"] not in {"<incomplete>", "00:00:00:00:00:00"}:
                gateway_macs.setdefault(gw, {}).setdefault(arp_entry["mac"], []).append(row["device"])
    for gw, by_mac in gateway_macs.items():
        if len(by_mac) > 1:
            desc = "; ".join(f"{mac} -> {hosts}" for mac, hosts in by_mac.items())
            findings.append(f"arp_gateway_divergence gateway={gw}: {desc}")

    return findings


def _text_summary(payload: dict[str, Any], show_clean: bool) -> str:
    lines = []
    lines.append("=== INFRA SWEEP ===")
    lines.append(f"Lab: {payload['lab_name']}")
    lines.append(f"Scope: {', '.join(payload['scope'])}")
    lines.append(f"Devices scanned: {payload['device_count']}")
    lines.append(f"Flagged devices: {payload['flagged_count']}")
    if payload["device_errors"]:
        lines.append(f"Device errors: {len(payload['device_errors'])}")
        for item in payload["device_errors"]:
            lines.append(f"  - {item['device']}: {item['error']}")

    # Always print what categories WERE inspected, with positive counts even
    # at zero — so the agent can tell "we looked and found nothing" from "we
    # didn't look." The previous compact form ("Flagged devices: 0") gave no
    # evidence of coverage and looked identical to a no-op run.
    nft_total = sum(c for _, c in payload["acl_fingerprints"])
    sample_arp_lines = []
    hosts_with_default = 0
    hosts_with_ipv4 = 0
    hosts_total = 0
    static_arp_count = 0
    fabricated_mac_count = 0
    for entry in (payload.get("flagged_devices", []) + payload.get("clean_devices", [])):
        if entry.get("role") != "host":
            continue
        hosts_total += 1
        if any(iface["ipv4"] for iface in entry.get("interfaces", [])):
            hosts_with_ipv4 += 1
        if entry.get("default_routes"):
            hosts_with_default += 1
        for arp in entry.get("arp", []):
            if arp.get("flag") in ("CM", "PM", "PERMANENT"):
                static_arp_count += 1
        for flag in entry.get("flags", []):
            if "fabricated_mac" in flag:
                fabricated_mac_count += 1
        if len(sample_arp_lines) < 3 and entry.get("arp"):
            arp_preview = ", ".join(
                f"{a['ip']}={a['mac']}({a.get('flag','')})" for a in entry["arp"][:3]
            )
            sample_arp_lines.append(f"  {entry['device']}: {arp_preview}")
    lines.append("")
    lines.append("Categories inspected:")
    lines.append(f"  - ACL/firewall drop fingerprints: {nft_total} device(s) with rules matched (0 means no protocol-specific drops seen)")
    lines.append(f"  - Static ARP entries (CM/PM/PERMANENT) on hosts: {static_arp_count} found")
    lines.append(f"  - Fabricated-pattern MACs in host ARP: {fabricated_mac_count} found")
    lines.append(f"  - Hosts with IPv4 / with default route: {hosts_with_ipv4}/{hosts_total}, {hosts_with_default}/{hosts_total}")
    cross = len(payload.get("cross_host_flags", []))
    lines.append(f"  - Cross-host flags (resolver/gateway divergence): {cross}")
    if sample_arp_lines:
        lines.append("Sample host ARP entries (first 3):")
        lines.extend(sample_arp_lines)
    # Note what is NOT in this sweep so the agent doesn't assume "clean = all-clear"
    lines.append("Note: this sweep does NOT scan for duplicate `link/ether` (use l2_snapshot for that).")

    if payload["acl_fingerprints"]:
        lines.append("ACL fingerprints found:")
        for name, count in payload["acl_fingerprints"]:
            lines.append(f"  - {name}: {count} device(s)")

    if payload["cross_host_flags"]:
        lines.append("Cross-host flags:")
        for flag in payload["cross_host_flags"]:
            lines.append(f"  - {flag}")

    if payload["flagged_devices"]:
        lines.append("")
        lines.append("Flagged device details:")
        for entry in payload["flagged_devices"]:
            lines.append(f"- {entry['device']} ({entry['role']})")
            for flag in entry["flags"]:
                lines.append(f"    flag: {flag}")
            if entry.get("nft_hits"):
                for hit in entry["nft_hits"]:
                    lines.append(f"    rule: {hit['rule']}")
            if entry["role"] == "host":
                default_text = ", ".join(r["raw"] for r in entry["default_routes"]) or "(none)"
                ipv4 = [f"{iface['ifname']}: {', '.join(iface['ipv4']) or '(none)'}" for iface in entry["interfaces"]]
                lines.append(f"    addrs: {'; '.join(ipv4)}")
                lines.append(f"    default: {default_text}")
                lines.append(f"    resolv: {', '.join(entry['nameservers']) or '(none)'}")

    if show_clean and payload.get("clean_devices"):
        lines.append("")
        lines.append("Clean devices (summary):")
        for entry in payload["clean_devices"]:
            nft_note = f"nft={len(entry['nft_hits'])}" if entry.get("nft_hits") is not None else "nft=?"
            lines.append(
                f"- {entry['device']} ({entry['role']}): {nft_note}, "
                f"routes={entry.get('route_count', '?')}, "
                f"resolv={','.join(entry.get('nameservers', [])) or '(none)'}"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Universal infrastructure sweep across every device.")
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
    parser.add_argument("--show-clean", action="store_true", help="Print a one-line summary for unflagged devices too.")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    KatharaAPI = _load_api_class()
    api = KatharaAPI(lab_name=args.lab)
    api.load_machines()

    groups = args.groups if args.groups else ([] if args.devices else DEFAULT_GROUPS)
    devices = _resolve_devices(api, args.devices, groups, expand_neighbors=False)

    group_index = {
        "hosts": api.hosts,
        "routers": api.routers,
        "switches": api.switches,
        "servers": dict(api.servers),
    }

    rows: list[dict[str, Any]] = []
    for device in devices:
        rows.append(_device_sweep(api, device, _role_of(device, group_index)))

    host_rows = [row for row in rows if row.get("role") == "host" and not row.get("error")]
    cross_host_flags = _cross_host_checks(host_rows) if host_rows else []
    if cross_host_flags:
        # Attach the cross-host signal to any host that shows resolver or gateway divergence,
        # so a reviewer can trace which host triggered the flag.
        for row in host_rows:
            row.setdefault("flags", [])
            for flag in cross_host_flags:
                row["flags"].append(f"cross_host: {flag.split(':', 1)[0]}")

    fingerprint_counter: Counter[str] = Counter()
    for row in rows:
        for hit in row.get("nft_hits") or []:
            fingerprint_counter[hit["classification"]] += 1

    flagged = [row for row in rows if row.get("flags")]
    clean = [row for row in rows if not row.get("flags") and not row.get("error")]
    device_errors = [{"device": row["device"], "error": row["error"]} for row in rows if row.get("error")]

    payload = {
        "lab_name": api.lab.name,
        "scope": args.devices if args.devices else groups,
        "device_count": len(devices),
        "flagged_count": len(flagged),
        "clean_count": len(clean),
        "device_errors": device_errors,
        "acl_fingerprints": sorted(fingerprint_counter.items()),
        "cross_host_flags": cross_host_flags,
        "flagged_devices": flagged,
        # Text summary needs all records to compute per-category counts and
        # ARP samples; JSON output filters them out for size reasons below.
        "clean_devices": clean,
        "clean_devices_omitted": False,
    }
    if args.as_json:
        # Drop clean_devices from JSON unless --show-clean (prevents l-size
        # JSON from ballooning past the oversized-output threshold).
        json_payload = dict(payload)
        if not args.show_clean:
            json_payload["clean_devices"] = []
            json_payload["clean_devices_omitted"] = bool(clean)
        print(json.dumps(json_payload, indent=2, default=str))
    else:
        print(_text_summary(payload, show_clean=args.show_clean), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
