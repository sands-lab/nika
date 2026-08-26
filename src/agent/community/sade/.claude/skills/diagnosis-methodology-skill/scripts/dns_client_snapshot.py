#!/usr/bin/env python
"""
Compact DNS client snapshot helper.

This script is designed to catch host-local DNS faults that stay invisible when
the agent only samples a few healthy endpoints. It scans a selected device set
(`hosts` by default), groups devices by resolver configuration, and verifies
hostname resolution either for a requested target or for the live published
service names discovered from the DNS server.

Primary use case:
- `host_incorrect_dns` in large DHCP-based enterprise topologies where only one
  host has a poisoned `/etc/resolv.conf`.
- `dns_record_error` during broad-search escalation when the caller provides no
  explicit hostname and still expects DNS correctness to be checked.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from typing import Any

from network_inventory import _lab_groups, _load_api_class, _resolve_devices


IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
ZONE_A_RECORD_RE = re.compile(r"^([A-Za-z0-9_-]+|@)\s+IN\s+A\s+((?:\d{1,3}\.){3}\d{1,3})\s*$")
ZONE_BLOCK_RE = re.compile(r'zone\s+"([^"]+)"\s+IN\s*\{.*?file\s+"([^"]+)";', re.IGNORECASE | re.DOTALL)
AUTO_FULL_LOOKUP_MAX_DEVICES = 12
AUTO_FULL_LOOKUP_MAX_HOSTNAMES = 8


def _command_failed(output: str) -> bool:
    return output.startswith("[TIMEOUT]") or output.startswith("Machine ") or "not found in lab" in output


def _safe_exec(api: Any, device: str, command: str) -> tuple[str, str | None]:
    output = api.exec_cmd(device, command)
    if _command_failed(output):
        return output, output
    return output, None


# Parse nameservers from resolv.conf content, ignoring comments and blank lines.
def _parse_nameservers(raw: str) -> list[str]:
    nameservers = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("nameserver "):
            nameservers.append(stripped.split(None, 1)[1])
    return nameservers


# Parse nslookup ouput to extract resolved IP addresses, handling different output formats and error cases.
def _parse_nslookup_addresses(raw: str) -> list[str]:
    addresses = []
    after_name = False
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("Name:"):
            after_name = True
            continue
        if not after_name:
            continue
        if stripped.startswith(("Address:", "Addresses:")):
            tail = stripped.split(":", 1)[1].strip()
            for match in IP_RE.findall(tail):
                if match not in addresses:
                    addresses.append(match)
            continue
        if IP_RE.fullmatch(stripped) and stripped not in addresses:
            addresses.append(stripped)
    return addresses


def _parse_zone_a_records(raw: str) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
    for line in raw.splitlines():
        stripped = line.split(";", 1)[0].strip()
        if not stripped:
            continue
        match = ZONE_A_RECORD_RE.match(stripped)
        if match:
            records.append((match.group(1), match.group(2)))
    return records


def _reverse_zone(zone: str) -> bool:
    normalized = zone.lower().rstrip(".")
    return normalized.endswith(".in-addr.arpa") or normalized.endswith(".ip6.arpa")


_BIND_DEFAULT_ZONES = frozenset({"0", "127", "255", "empty", "root"})


def _zone_from_path(path: str) -> str | None:
    filename = path.rsplit("/", 1)[-1].strip()
    if not filename or filename.endswith(".bak"):
        return None
    if filename.startswith("db."):
        zone = filename[3:]
    elif filename.endswith(".zone"):
        zone = filename[:-5]
    else:
        return None
    if not zone or _reverse_zone(zone):
        return None
    # Skip bind9 default empty-zone files (db.0, db.127, db.255, db.empty,
    # db.root). They're not real service zones; including them would generate
    # bogus hostnames like "web0.127" from the inferred-hostname path.
    if zone in _BIND_DEFAULT_ZONES:
        return None
    return zone


def _bind_zone_sources(api: Any, dns_device: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for config_path in (
        "/etc/bind/named.conf",
        "/etc/bind/named.conf.local",
        "/etc/bind/named.conf.default-zones",
    ):
        config_raw, config_error = _safe_exec(api, dns_device, f"cat {config_path} 2>/dev/null")
        if config_error:
            continue
        for zone, path in ZONE_BLOCK_RE.findall(config_raw):
            normalized_zone = zone.strip().rstrip(".")
            normalized_path = path.strip()
            if not normalized_zone or not normalized_path or _reverse_zone(normalized_zone):
                continue
            key = (normalized_zone, normalized_path)
            if key not in seen:
                seen.add(key)
                candidates.append(key)

    file_list_raw, file_list_error = _safe_exec(api, dns_device, "find /etc/bind -maxdepth 2 -type f 2>/dev/null")
    if file_list_error:
        return candidates

    for raw_path in file_list_raw.splitlines():
        path = raw_path.strip()
        if not path:
            continue
        zone = _zone_from_path(path)
        if not zone:
            continue
        key = (zone, path)
        if key not in seen:
            seen.add(key)
            candidates.append(key)

    return candidates


def _resolver_key(nameservers: list[str]) -> str:
    return ",".join(nameservers) if nameservers else "(none)"


def _infer_expected_nameserver(records: list[dict[str, Any]]) -> tuple[str | None, int]:
    counter = Counter()
    for record in records:
        if record["nameservers"]:
            counter[record["nameservers"][0]] += 1
    if not counter:
        return None, 0
    expected, count = counter.most_common(1)[0]
    return expected, count


def _pick_hosts_for_dns_checks(
    records: list[dict[str, Any]],
    suspect_hosts: list[str],
    healthy_sample_size: int,
    per_resolver_group: bool = False,
) -> list[str]:
    selected = list(suspect_hosts)
    if healthy_sample_size <= 0:
        return list(dict.fromkeys(selected))

    healthy_hosts = [record for record in records if record["device"] not in suspect_hosts and not record["error"]]
    if per_resolver_group:
        grouped: dict[str, list[str]] = defaultdict(list)
        for record in healthy_hosts:
            grouped[record["resolver_key"]].append(record["device"])
        for devices in grouped.values():
            selected.extend(devices[:healthy_sample_size])
    else:
        selected.extend(record["device"] for record in healthy_hosts[:healthy_sample_size])
    return list(dict.fromkeys(selected))


def _default_service_devices(api: Any) -> list[str]:
    inventory = _lab_groups(api)
    devices: list[str] = []
    devices.extend(sorted(inventory["servers"].get("web", [])))
    devices.extend(sorted(inventory["servers"].get("load_balancer", [])))
    return list(dict.fromkeys(devices))


def _device_ip(api: Any, device: str) -> str | None:
    try:
        ip = api.get_host_ip(device, with_prefix=False)
    except TypeError:
        ip = api.get_host_ip(device)
    except Exception:
        return None
    if not ip:
        return None
    return ip.split("/", 1)[0]


def _expected_service_ips(api: Any) -> set[str]:
    addresses: set[str] = set()
    for device in _default_service_devices(api):
        if ip := _device_ip(api, device):
            addresses.add(ip)
    return addresses


def _inferred_service_hostnames(api: Any, zones: list[str]) -> list[str]:
    hostnames: list[str] = []
    service_devices = _default_service_devices(api)

    for zone in zones:
        normalized_zone = zone.strip().rstrip(".")
        if not normalized_zone or _reverse_zone(normalized_zone):
            continue
        for device in service_devices:
            device_l = device.lower()
            if "load_balancer" in device_l:
                hostname = f"web99.{normalized_zone}"
            else:
                web_index = _web_index_from_device(device_l)
                if web_index is None:
                    continue
                hostname = f"web{web_index}.{normalized_zone}"
            if hostname not in hostnames:
                hostnames.append(hostname)

    return hostnames


def _published_hostnames(api: Any) -> list[str]:
    inventory = _lab_groups(api)
    dns_devices = sorted(inventory["servers"].get("dns", []))
    hostnames: list[str] = []
    inferred_hostnames: list[str] = []
    expected_service_ips = _expected_service_ips(api)

    for dns_device in dns_devices:
        zone_sources = _bind_zone_sources(api, dns_device)
        zone_names = [zone for zone, _path in zone_sources]
        for hostname in _inferred_service_hostnames(api, zone_names):
            if hostname not in inferred_hostnames:
                inferred_hostnames.append(hostname)

        for zone, path in zone_sources:
            zone_raw, zone_error = _safe_exec(api, dns_device, f"cat {path} 2>/dev/null")
            if zone_error:
                continue
            for label, record_ip in _parse_zone_a_records(zone_raw):
                lowered = label.lower()
                if label == "@" or lowered.startswith("ns"):
                    continue
                if expected_service_ips and record_ip not in expected_service_ips and not lowered.startswith("web"):
                    continue
                hostname = f"{label}.{zone}"
                if hostname not in hostnames:
                    hostnames.append(hostname)

    if hostnames:
        for hostname in inferred_hostnames:
            if hostname not in hostnames:
                hostnames.append(hostname)
        return hostnames
    return inferred_hostnames


def _hostname_tokens(hostname: str) -> tuple[str, list[str]]:
    label, _, zone = hostname.lower().partition(".")
    zone_tokens = [token for token in re.split(r"[^a-z0-9]+", zone) if token]
    return label, zone_tokens


def _web_index_from_label(label: str) -> str | None:
    match = re.match(r"web(\d+)", label)
    return match.group(1) if match else None


def _web_index_from_device(device: str) -> str | None:
    for pattern in (r"web_server_(\d+)", r"webserver(\d+)", r"web_(\d+)"):
        match = re.search(pattern, device.lower())
        if match:
            return match.group(1)
    return None


def _device_matches_hostname(device: str, hostname: str) -> bool:
    device_l = device.lower()
    label, zone_tokens = _hostname_tokens(hostname)
    label_index = _web_index_from_label(label)

    if "load_balancer" in device_l:
        return label.startswith("web") and label.endswith("99")

    if "web" not in label or "web" not in device_l:
        return False

    device_index = _web_index_from_device(device_l)
    if label_index and device_index and label_index != device_index:
        return False
    if label_index and not device_index:
        return False

    scoped_zone_tokens = [token for token in zone_tokens if token.startswith("pod") or any(ch.isdigit() for ch in token)]
    if scoped_zone_tokens and not all(token in device_l for token in scoped_zone_tokens):
        return False

    return True


def _expected_addresses_by_hostname(api: Any, hostnames: list[str]) -> dict[str, str]:
    service_devices = _default_service_devices(api)
    ip_by_device = {device: ip for device in service_devices if (ip := _device_ip(api, device))}
    expected: dict[str, str] = {}

    for hostname in hostnames:
        candidates = [device for device in service_devices if _device_matches_hostname(device, hostname)]
        candidates = [device for device in candidates if device in ip_by_device]
        if len(candidates) == 1:
            expected[hostname] = ip_by_device[candidates[0]]

    return expected


def _nslookup_snapshot(
    api: Any,
    device: str,
    hostname: str,
    expected_address: str | None,
    expected_service_ips: set[str],
) -> dict[str, Any]:
    raw, error = _safe_exec(api, device, f"timeout 5 nslookup {hostname} 2>&1")
    addresses = _parse_nslookup_addresses(raw) if not error else []
    flags: list[str] = []
    if error:
        flags.append("nslookup_failed")
    if not addresses:
        flags.append("nslookup_no_addresses")
    if expected_address and addresses and expected_address not in addresses:
        flags.append("nslookup_wrong_address")
    elif not expected_address and addresses and expected_service_ips and not set(addresses).issubset(expected_service_ips):
        flags.append("nslookup_unexpected_service_address")
    return {
        "device": device,
        "hostname": hostname,
        "addresses": addresses,
        "error": error,
        "flags": flags,
        "raw": raw,
        "expected_address": expected_address,
    }


def _lookup_signature(lookup: dict[str, Any]) -> str:
    if lookup["flags"]:
        return "problem:" + ",".join(sorted(lookup["flags"]))
    if lookup["addresses"]:
        return "answer:" + ",".join(lookup["addresses"])
    return "answer:(none)"


def _text_summary(payload: dict[str, Any]) -> str:
    lines = []
    lines.append("=== DNS CLIENT SNAPSHOT ===")
    lines.append(f"Lab: {payload['lab_name']}")
    lines.append(f"Devices scanned: {len(payload['devices'])}")
    lines.append(f"Expected nameserver: {payload['expected_nameserver'] or '(unknown)'}")
    if payload["expected_nameserver_count"]:
        lines.append(
            f"Expected nameserver support: {payload['expected_nameserver_count']}/{len(payload['devices'])} devices"
        )
    if payload["hostnames_checked"]:
        lines.append(f"Hostnames checked: {', '.join(payload['hostnames_checked'])}")
    if payload["lookup_devices"]:
        lines.append(f"Lookup devices: {', '.join(payload['lookup_devices'])}")
    if payload["lookup_mode"]:
        lines.append(f"Lookup mode: {payload['lookup_mode']}")
    if payload["auto_discovered_hostnames"]:
        lines.append("Hostname mode: auto-discovered from live DNS zones")
    elif payload["hostnames_checked"]:
        lines.append("Hostname mode: explicit target")
    lines.append("")

    # Cross-client resolver grouping and lookup-outcome grouping are produced
    # by service_snapshot's triage view; this script does not re-emit them.
    # Our unique contribution is the majority-resolver inference (above) and
    # the per-host per-name nslookup detail (below).

    if payload["suspect_hosts"]:
        lines.append("Suspect hosts:")
        for record in payload["devices"]:
            if record["device"] not in payload["suspect_hosts"]:
                continue
            nameserver_text = ", ".join(record["nameservers"]) if record["nameservers"] else "(none)"
            flags_text = ", ".join(record["flags"]) if record["flags"] else "none"
            lines.append(f"- {record['device']}: nameservers={nameserver_text} flags={flags_text}")
            for hostname, lookup in record["nslookups"].items():
                addr_text = ", ".join(lookup["addresses"]) if lookup["addresses"] else "(none)"
                lookup_flags = ", ".join(lookup["flags"]) if lookup["flags"] else "none"
                expected_text = lookup["expected_address"] or "(service-pool check)"
                lines.append(
                    f"  nslookup {hostname}: addresses={addr_text} expected={expected_text} flags={lookup_flags}"
                )
    else:
        lines.append("Suspect hosts: none")

    healthy_checks = [
        record for record in payload["devices"]
        if record["nslookups"] and record["device"] not in payload["suspect_hosts"]
    ]
    if healthy_checks:
        lines.append("")
        lines.append("Healthy verification sample:")
        for record in healthy_checks:
            lines.append(f"- {record['device']}:")
            for hostname, lookup in record["nslookups"].items():
                addr_text = ", ".join(lookup["addresses"]) if lookup["addresses"] else "(none)"
                expected_text = lookup["expected_address"] or "(service-pool check)"
                lines.append(f"  {hostname} -> {addr_text} expected={expected_text}")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Compact DNS client snapshot helper.")
    parser.add_argument("--lab", default=os.getenv("LAB_NAME", "campus_lan"))
    parser.add_argument("hostname_arg", nargs="?", help="Optional hostname shortcut.")
    parser.add_argument("--device", action="append", default=[], dest="devices")
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        choices=["hosts", "routers", "switches", "servers", "bmv2_switches", "ovs_switches", "sdn_controllers", "all"],
        dest="groups",
    )
    parser.add_argument("--expected-nameserver")
    parser.add_argument("--hostname")
    parser.add_argument("--expected-address")
    parser.add_argument("--target-device")
    parser.add_argument("--healthy-sample-size", type=int, default=1)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    if args.hostname and args.hostname_arg:
        parser.error("Use either the positional hostname or --hostname, not both.")
    hostname = args.hostname or args.hostname_arg

    KatharaAPI = _load_api_class()
    api = KatharaAPI(lab_name=args.lab)
    devices = _resolve_devices(api, args.devices, args.groups or ["hosts"], expand_neighbors=False)
    expected_service_ips = _expected_service_ips(api)
    hostnames_to_check = [hostname] if hostname else _published_hostnames(api)
    auto_discovered_hostnames = not hostname and bool(hostnames_to_check)

    expected_addresses: dict[str, str] = {}
    if hostname:
        expected_address = args.expected_address
        if not expected_address and args.target_device:
            try:
                expected_address = api.get_host_ip(args.target_device)
            except Exception:
                expected_address = None
        if expected_address:
            expected_addresses[hostname] = expected_address
    elif hostnames_to_check:
        expected_addresses = _expected_addresses_by_hostname(api, hostnames_to_check)

    records: list[dict[str, Any]] = []
    for device in devices:
        resolv_raw, error = _safe_exec(api, device, "cat /etc/resolv.conf 2>/dev/null")
        nameservers = _parse_nameservers(resolv_raw) if not error else []
        records.append(
            {
                "device": device,
                "nameservers": nameservers,
                "resolver_key": _resolver_key(nameservers),
                "error": error,
                "flags": ["resolver_collection_failed"] if error else [],
                "records": {"resolv_conf": resolv_raw},
                "nslookups": {},
            }
        )

    expected_nameserver = args.expected_nameserver
    expected_nameserver_count = 0
    if expected_nameserver:
        expected_nameserver_count = sum(
            1 for record in records if expected_nameserver in record["nameservers"]
        )
    else:
        expected_nameserver, expected_nameserver_count = _infer_expected_nameserver(records)

    suspect_hosts: list[str] = []
    for record in records:
        if record["error"]:
            suspect_hosts.append(record["device"])
            continue
        if not record["nameservers"]:
            record["flags"].append("no_nameserver")
            suspect_hosts.append(record["device"])
            continue
        if expected_nameserver and expected_nameserver not in record["nameservers"]:
            record["flags"].append("unexpected_nameserver")
            suspect_hosts.append(record["device"])

    lookup_hosts: list[str] = []
    lookup_mode = "none"
    if hostnames_to_check:
        auto_full_lookup = (
            auto_discovered_hostnames
            and len(records) <= AUTO_FULL_LOOKUP_MAX_DEVICES
            and len(hostnames_to_check) <= AUTO_FULL_LOOKUP_MAX_HOSTNAMES
        )
        if auto_full_lookup:
            lookup_hosts = [record["device"] for record in records if not record["error"]]
            lookup_mode = "full client sweep"
        else:
            lookup_hosts = _pick_hosts_for_dns_checks(
                records,
                suspect_hosts,
                max(args.healthy_sample_size, 0),
                per_resolver_group=auto_discovered_hostnames,
            )
            lookup_mode = "resolver-group sample" if auto_discovered_hostnames else "targeted sample"
        for record in records:
            if record["device"] not in lookup_hosts:
                continue
            for current_hostname in hostnames_to_check:
                lookup = _nslookup_snapshot(
                    api,
                    record["device"],
                    current_hostname,
                    expected_addresses.get(current_hostname),
                    expected_service_ips,
                )
                record["nslookups"][current_hostname] = lookup
                if lookup["flags"] and record["device"] not in suspect_hosts:
                    suspect_hosts.append(record["device"])

        for current_hostname in hostnames_to_check:
            signature_counter = Counter(
                _lookup_signature(record["nslookups"][current_hostname])
                for record in records
                if current_hostname in record["nslookups"]
            )
            if len(signature_counter) <= 1:
                continue
            majority_signature, _ = signature_counter.most_common(1)[0]
            for record in records:
                lookup = record["nslookups"].get(current_hostname)
                if not lookup:
                    continue
                if _lookup_signature(lookup) == majority_signature:
                    continue
                if "nslookup_inconsistent_with_peer_group" not in lookup["flags"]:
                    lookup["flags"].append("nslookup_inconsistent_with_peer_group")
                if record["device"] not in suspect_hosts:
                    suspect_hosts.append(record["device"])

    resolver_groups: dict[str, list[str]] = defaultdict(list)
    for record in records:
        resolver_groups[record["resolver_key"]].append(record["device"])

    lookup_groups: dict[str, dict[str, list[str]]] = {}
    for current_hostname in hostnames_to_check:
        grouped: dict[str, list[str]] = defaultdict(list)
        for record in records:
            lookup = record["nslookups"].get(current_hostname)
            if not lookup:
                continue
            grouped[_lookup_signature(lookup)].append(record["device"])
        if grouped:
            lookup_groups[current_hostname] = {
                key: sorted(value) for key, value in sorted(grouped.items())
            }

    payload = {
        "lab_name": api.lab.name,
        "hostnames_checked": hostnames_to_check,
        "lookup_devices": lookup_hosts,
        "lookup_mode": lookup_mode,
        "auto_discovered_hostnames": auto_discovered_hostnames,
        "expected_nameserver": expected_nameserver,
        "expected_nameserver_count": expected_nameserver_count,
        "suspect_hosts": sorted(set(suspect_hosts)),
        "resolver_groups": {key: sorted(value) for key, value in sorted(resolver_groups.items())},
        "lookup_groups": lookup_groups,
        "devices": records,
    }

    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(_text_summary(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
