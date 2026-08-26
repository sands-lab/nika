#!/usr/bin/env python
"""
Compact service-path snapshot helper.

This combines the common "upper-layer canary" checks into one pass:
- client-side resolvers
- hostname resolution
- HTTP timing by URL
- optional localhost HTTP checks on service devices
- optional lightweight service-process visibility

Use it for fast DNS/LB/web coverage after the basic path is already understood.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from statistics import mean
from typing import Any

from network_inventory import _lab_groups, _load_api_class, _resolve_devices


IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
CURL_FIELD_RE = re.compile(r"(http_code|remote_ip|namelookup|connect|starttransfer|total):([^\s]+)")
ZONE_A_RECORD_RE = re.compile(r"^([A-Za-z0-9_-]+|@)\s+IN\s+A\s+((?:\d{1,3}\.){3}\d{1,3})\s*$")
ZONE_BLOCK_RE = re.compile(r'zone\s+"([^"]+)"\s+IN\s*\{.*?file\s+"([^"]+)";', re.IGNORECASE | re.DOTALL)
SERVICE_PROC_PATTERNS = "nginx|haproxy|apache2|httpd|python3|named|dnsmasq|dhcpd"
DEFAULT_CLIENT_SAMPLE = 4
AUTO_FULL_CLIENT_MAX = 12
AUTO_COMPACT_CLIENT_THRESHOLD = 8


def _command_failed(output: str) -> bool:
    return output.startswith("[TIMEOUT]") or output.startswith("Machine ") or "not found in lab" in output


def _safe_exec(api: Any, device: str, command: str) -> tuple[str, str | None]:
    output = api.exec_cmd(device, command)
    if _command_failed(output):
        return output, output
    return output, None


def _parse_nameservers(raw: str) -> list[str]:
    nameservers = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("nameserver "):
            nameservers.append(stripped.split(None, 1)[1])
    return nameservers


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


def _hostname_from_url(url: str) -> str | None:
    target = url
    if "://" in target:
        target = target.split("://", 1)[1]
    host = target.split("/", 1)[0].split(":", 1)[0].strip()
    return host or None


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
    # Skip bind9 default files (db.0, db.127, db.255, db.empty, db.root).
    # These are not real service zones; including them makes the helper
    # manufacture fake hostnames like "web0.127" that bloat output without
    # adding any diagnostic signal.
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


def _curl_command(url: str, times: int) -> str:
    return (
        f"for i in $(seq 1 {times}); do "
        f"curl -sS --connect-timeout 5 --max-time 10 "
        f"-o /dev/null "
        f"-w 'http_code:%{{http_code}} remote_ip:%{{remote_ip}} namelookup:%{{time_namelookup}} "
        f"connect:%{{time_connect}} starttransfer:%{{time_starttransfer}} total:%{{time_total}}\\n' "
        f"'{url}' || echo 'curl_failed'; "
        f"done"
    )


def _default_web_service_devices(api: Any) -> list[str]:
    inventory = _lab_groups(api)
    devices: list[str] = []
    devices.extend(sorted(inventory["servers"].get("web", [])))
    devices.extend(sorted(inventory["servers"].get("load_balancer", [])))
    return list(dict.fromkeys(devices))


def _device_http_url(api: Any, device: str) -> str | None:
    try:
        ip = api.get_host_ip(device, with_prefix=False)
    except TypeError:
        ip = api.get_host_ip(device)
    except Exception:
        return None
    if not ip:
        return None
    if "/" in ip:
        ip = ip.split("/", 1)[0]
    return f"http://{ip}/"


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
    for device in _default_web_service_devices(api):
        if ip := _device_ip(api, device):
            addresses.add(ip)
    return addresses


def _web_index_from_device(device: str) -> str | None:
    for pattern in (r"web_server_(\d+)", r"webserver(\d+)", r"web_(\d+)"):
        match = re.search(pattern, device.lower())
        if match:
            return match.group(1)
    return None


def _hostname_tokens(hostname: str) -> tuple[str, list[str]]:
    label, _, zone = hostname.lower().partition(".")
    zone_tokens = [token for token in re.split(r"[^a-z0-9]+", zone) if token]
    return label, zone_tokens


def _web_index_from_label(label: str) -> str | None:
    match = re.match(r"web(\d+)", label)
    return match.group(1) if match else None


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
    service_devices = _default_web_service_devices(api)
    ip_by_device = {device: ip for device in service_devices if (ip := _device_ip(api, device))}
    expected: dict[str, str] = {}

    for hostname in hostnames:
        candidates = [device for device in service_devices if _device_matches_hostname(device, hostname)]
        candidates = [device for device in candidates if device in ip_by_device]
        if len(candidates) == 1:
            expected[hostname] = ip_by_device[candidates[0]]

    return expected


def _inferred_service_hostnames(api: Any, zones: list[str]) -> list[str]:
    hostnames: list[str] = []
    service_devices = _default_web_service_devices(api)

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


def _published_hostnames(api: Any, expected_service_ips: set[str]) -> list[str]:
    inventory = _lab_groups(api)
    dns_devices = sorted(inventory["servers"].get("dns", []))
    hostnames: list[str] = []
    inferred_hostnames: list[str] = []

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

            for label, ip in _parse_zone_a_records(zone_raw):
                if label == "@":
                    continue
                normalized = label.lower()
                if (expected_service_ips and ip in expected_service_ips) or normalized.startswith("web"):
                    hostname = f"{label}.{zone}"
                    if hostname not in hostnames:
                        hostnames.append(hostname)

    if hostnames:
        for hostname in inferred_hostnames:
            if hostname not in hostnames:
                hostnames.append(hostname)
        return hostnames
    return inferred_hostnames


def _parse_curl_samples(raw: str) -> tuple[list[dict[str, Any]], list[str]]:
    samples: list[dict[str, Any]] = []
    errors: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "curl_failed" or stripped.startswith("curl:"):
            errors.append(stripped)
            continue
        fields = dict(CURL_FIELD_RE.findall(stripped))
        if not fields:
            errors.append(stripped)
            continue
        sample: dict[str, Any] = {"raw": stripped}
        for key, value in fields.items():
            if key == "http_code":
                sample[key] = value
            else:
                try:
                    sample[key] = float(value)
                except ValueError:
                    sample[key] = value
        samples.append(sample)
    return samples, errors


def _http_summary(raw: str) -> dict[str, Any]:
    samples, errors = _parse_curl_samples(raw)
    totals = [sample["total"] for sample in samples if isinstance(sample.get("total"), float)]
    remote_ips = sorted({sample["remote_ip"] for sample in samples if sample.get("remote_ip")})
    codes = sorted({sample["http_code"] for sample in samples if sample.get("http_code")})
    return {
        "samples": samples,
        "errors": errors,
        "http_codes": codes,
        "remote_ips": remote_ips,
        "avg_total": round(mean(totals), 3) if totals else None,
    }


def _dns_signature(result: dict[str, Any]) -> str:
    if result["flags"]:
        return "problem:" + ",".join(sorted(result["flags"]))
    if result["addresses"]:
        return "answer:" + ",".join(result["addresses"])
    return "answer:(none)"


def _http_signature(result: dict[str, Any]) -> str:
    if result.get("flags"):
        return "problem:" + ",".join(sorted(result["flags"]))
    code_text = ",".join(result["http_codes"]) if result["http_codes"] else "(none)"
    remote_text = ",".join(result["remote_ips"]) if result["remote_ips"] else "(none)"
    return f"codes:{code_text} remote:{remote_text}"


def _expected_remote_ip_for_url(
    url: str,
    expected_addresses_by_hostname: dict[str, str],
    expected_service_ips: set[str],
) -> str | None:
    hostname = _hostname_from_url(url)
    if not hostname:
        return None
    if IP_RE.fullmatch(hostname):
        return hostname if hostname in expected_service_ips else None
    return expected_addresses_by_hostname.get(hostname)


def _client_snapshot(
    api: Any,
    device: str,
    hostnames: list[str],
    urls: list[str],
    times: int,
    published_hostnames: set[str],
    expected_service_ips: set[str],
    expected_addresses_by_hostname: dict[str, str],
) -> dict[str, Any]:
    resolv_raw, resolv_error = _safe_exec(api, device, "cat /etc/resolv.conf 2>/dev/null")
    nameservers = _parse_nameservers(resolv_raw) if not resolv_error else []
    dns_results: dict[str, Any] = {}
    http_results: dict[str, Any] = {}
    flags: list[str] = []

    for hostname in hostnames:
        raw, error = _safe_exec(api, device, f"nslookup {hostname} 2>&1")
        addresses = _parse_nslookup_addresses(raw) if not error else []
        dns_flags: list[str] = []
        expected_address = expected_addresses_by_hostname.get(hostname)
        if error:
            dns_flags.append("lookup_failed")
        if not addresses:
            dns_flags.append("no_addresses")
        if expected_address and addresses and expected_address not in addresses:
            dns_flags.append("wrong_address")
        elif hostname in published_hostnames and addresses and expected_service_ips and not set(addresses).issubset(expected_service_ips):
            dns_flags.append("unexpected_service_address")
        dns_results[hostname] = {
            "addresses": addresses,
            "error": error,
            "flags": dns_flags,
            "raw": raw,
            "expected_address": expected_address,
        }
        if error or not addresses:
            flags.append(f"dns_problem:{hostname}")
        if "unexpected_service_address" in dns_flags or "wrong_address" in dns_flags:
            flags.append(f"dns_unexpected_address:{hostname}")

    for url in urls:
        expected_remote_ip = _expected_remote_ip_for_url(url, expected_addresses_by_hostname, expected_service_ips)
        raw, error = _safe_exec(api, device, _curl_command(url, times))
        summary = _http_summary(raw) if not error else {
            "samples": [],
            "errors": [error],
            "http_codes": [],
            "remote_ips": [],
            "avg_total": None,
        }
        http_flags: list[str] = []
        if error or not summary["samples"]:
            http_flags.append("http_problem")
        elif any(code == "000" or not code.startswith(("2", "3")) for code in summary["http_codes"]):
            http_flags.append("http_non_ok")
        if expected_remote_ip and summary["remote_ips"] and any(ip != expected_remote_ip for ip in summary["remote_ips"]):
            http_flags.append("wrong_remote_ip")
        elif not expected_remote_ip and summary["remote_ips"] and expected_service_ips and any(ip not in expected_service_ips for ip in summary["remote_ips"]):
            http_flags.append("unexpected_service_remote_ip")
        http_results[url] = {
            "error": error,
            "expected_remote_ip": expected_remote_ip,
            "flags": http_flags,
            **summary,
        }
        if error or not summary["samples"]:
            flags.append(f"http_problem:{url}")
        elif any(code == "000" or not code.startswith(("2", "3")) for code in summary["http_codes"]):
            flags.append(f"http_non_ok:{url}")
        if "wrong_remote_ip" in http_flags or "unexpected_service_remote_ip" in http_flags:
            flags.append(f"http_wrong_remote:{url}")

    return {
        "device": device,
        "nameservers": nameservers,
        "resolver_key": ",".join(nameservers) if nameservers else "(none)",
        "dns": dns_results,
        "http": http_results,
        "flags": flags,
    }


def _service_device_snapshot(api: Any, device: str, localhost_url: str, times: int, include_processes: bool) -> dict[str, Any]:
    http_raw, http_error = _safe_exec(api, device, _curl_command(localhost_url, times))
    http_summary = _http_summary(http_raw) if not http_error else {"samples": [], "errors": [http_error], "http_codes": [], "avg_total": None}
    proc_lines: list[str] = []
    proc_error: str | None = None
    if include_processes:
        proc_raw, proc_error = _safe_exec(
            api,
            device,
            f"ps aux | grep -E '{SERVICE_PROC_PATTERNS}' | grep -v grep | head -20",
        )
        if not proc_error:
            proc_lines = [line for line in proc_raw.splitlines() if line.strip()]

    flags: list[str] = []
    if http_error or not http_summary["samples"]:
        flags.append("localhost_http_problem")
    elif any(code == "000" or not code.startswith(("2", "3")) for code in http_summary["http_codes"]):
        flags.append("localhost_http_non_ok")
    # `slow_localhost_http`: when localhost HTTP returns 2xx but takes seconds
    # to complete (or hits curl's --max-time), the application is responding
    # slowly even though the network and the response code look fine. This is
    # the in-application-delay fingerprint (e.g. a server that sleeps inside
    # its handler) — distinct from network or service-down faults. Threshold
    # is conservative: a localhost call that takes >1s on a Kathara container
    # is far outside any healthy baseline.
    avg = http_summary.get("avg_total")
    if avg is not None and isinstance(avg, (int, float)) and avg >= 1.0:
        flags.append(f"slow_localhost_http: avg_total={avg:.2f}s")
    if include_processes and proc_error:
        flags.append("process_check_failed")

    return {
        "device": device,
        "localhost_url": localhost_url,
        "http": {
            "error": http_error,
            **http_summary,
        },
        "processes": proc_lines,
        "process_error": proc_error,
        "flags": flags,
    }


def _text_summary(payload: dict[str, Any]) -> str:
    lines = []
    lines.append("=== SERVICE SNAPSHOT ===")
    lines.append(f"Lab: {payload['lab_name']}")
    if payload.get("client_sampled"):
        lines.append(
            f"Clients scanned: {len(payload['clients'])} "
            f"(sample of {payload['all_host_count']} hosts)"
        )
        lines.append(
            "Note: sampled client timing is triage only; confirm same-role peer ownership locally before naming the faulty device."
        )
    else:
        lines.append(f"Clients scanned: {len(payload['clients'])}")
    lines.append(f"Service devices scanned: {len(payload['service_devices'])}")
    lines.append(f"Hostnames: {', '.join(payload['hostnames']) if payload['hostnames'] else '(none)'}")
    lines.append(f"URLs: {', '.join(payload['urls']) if payload['urls'] else '(none)'}")
    if payload["published_hostnames"]:
        lines.append(f"Published hostnames: {', '.join(payload['published_hostnames'])}")
    if payload["missing_hostnames"]:
        lines.append(f"Untested published hostnames: {', '.join(payload['missing_hostnames'])}")
    if payload["coverage_warnings"]:
        lines.append(f"Coverage warnings: {', '.join(payload['coverage_warnings'])}")
    lines.append("")

    if payload["resolver_groups"]:
        lines.append("Resolver groups:")
        for resolver_key, devices in payload["resolver_groups"].items():
            preview = ", ".join(devices[:8])
            suffix = "" if len(devices) <= 8 else f" ... (+{len(devices) - 8} more)"
            lines.append(f"- {resolver_key}: {len(devices)} hosts [{preview}{suffix}]")
        lines.append("")

    if payload["dns_groups"]:
        lines.append("DNS outcome groups:")
        for hostname, groups in payload["dns_groups"].items():
            lines.append(f"- {hostname}:")
            for signature, devices in groups.items():
                preview = ", ".join(devices[:8])
                suffix = "" if len(devices) <= 8 else f" ... (+{len(devices) - 8} more)"
                lines.append(f"  {signature}: {len(devices)} hosts [{preview}{suffix}]")
        lines.append("")

    if payload["http_groups"]:
        lines.append("HTTP outcome groups:")
        for url, groups in payload["http_groups"].items():
            lines.append(f"- {url}:")
            for signature, devices in groups.items():
                preview = ", ".join(devices[:8])
                suffix = "" if len(devices) <= 8 else f" ... (+{len(devices) - 8} more)"
                lines.append(f"  {signature}: {len(devices)} hosts [{preview}{suffix}]")
        lines.append("")

    lines.append(f"Suspect clients: {', '.join(payload['suspect_clients']) if payload['suspect_clients'] else 'none'}")
    lines.append("")

    compact = payload.get("compact", False)
    if compact:
        trigger = "auto" if payload.get("auto_compact") else "requested"
        lines.append(
            f"(compact={trigger}: suppressing per-client/per-service blocks; "
            "re-run with --full for complete rows)"
        )
        lines.append("")

        suspect_set = set(payload["suspect_clients"])
        suspect_clients = [client for client in payload["clients"] if client["device"] in suspect_set]
        if suspect_clients:
            lines.append("Suspect client details:")
            for client in suspect_clients[:6]:
                lines.append(f"- {client['device']}: flags={', '.join(client['flags']) or 'none'}")
            if len(suspect_clients) > 6:
                lines.append(f"  ... (+{len(suspect_clients) - 6} more suspect clients)")
            lines.append("")

        for device in payload["service_devices"]:
            code_text = ", ".join(device["http"]["http_codes"]) if device["http"]["http_codes"] else "(none)"
            avg_text = (
                f"{device['http']['avg_total']:.3f}s"
                if isinstance(device["http"]["avg_total"], float)
                else "(none)"
            )
            flag_text = ", ".join(device["flags"]) if device["flags"] else "none"
            lines.append(
                f"Service device {device['device']}: codes={code_text} avg={avg_text} "
                f"procs={len(device['processes'])} flags={flag_text}"
            )
        if payload["service_devices"]:
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    for client in payload["clients"]:
        lines.append(client["device"])
        lines.append(f"  Nameservers: {', '.join(client['nameservers']) if client['nameservers'] else '(none)'}")
        for hostname, result in client["dns"].items():
            addr_text = ", ".join(result["addresses"]) if result["addresses"] else "(none)"
            dns_flags = ", ".join(result["flags"]) if result["flags"] else "none"
            expected_text = result.get("expected_address") or "(service-pool check)"
            lines.append(f"  DNS {hostname}: {addr_text} expected={expected_text} flags={dns_flags}")
        for url, result in client["http"].items():
            code_text = ", ".join(result["http_codes"]) if result["http_codes"] else "(none)"
            remote_text = ", ".join(result["remote_ips"]) if result["remote_ips"] else "(none)"
            avg_text = f"{result['avg_total']:.3f}s" if isinstance(result["avg_total"], float) else "(none)"
            expected_remote = result.get("expected_remote_ip") or "(service-pool check)"
            http_flags = ", ".join(result["flags"]) if result["flags"] else "none"
            lines.append(
                f"  HTTP {url}: codes={code_text} remote_ips={remote_text} expected_remote={expected_remote} "
                f"avg_total={avg_text} flags={http_flags}"
            )
        lines.append(f"  Flags: {', '.join(client['flags']) if client['flags'] else 'none'}")
        lines.append("")

    for device in payload["service_devices"]:
        code_text = ", ".join(device["http"]["http_codes"]) if device["http"]["http_codes"] else "(none)"
        avg_text = f"{device['http']['avg_total']:.3f}s" if isinstance(device["http"]["avg_total"], float) else "(none)"
        lines.append(device["device"])
        lines.append(f"  Localhost HTTP: codes={code_text} avg_total={avg_text}")
        lines.append(f"  Processes visible: {len(device['processes'])}")
        lines.append(f"  Flags: {', '.join(device['flags']) if device['flags'] else 'none'}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Compact service-path snapshot helper.")
    parser.add_argument("--lab", default=os.getenv("LAB_NAME", "campus_lan"))
    parser.add_argument("target", nargs="?", help="Optional hostname or full URL shortcut.")
    parser.add_argument("--client", action="append", default=[], dest="clients")
    parser.add_argument("--hostname", action="append", default=[], dest="hostnames")
    parser.add_argument("--url", action="append", default=[], dest="urls")
    parser.add_argument("--service-device", action="append", default=[], dest="service_devices")
    parser.add_argument("--localhost-url", default="http://127.0.0.1/")
    parser.add_argument("--times", type=int, default=2)
    parser.add_argument("--no-processes", action="store_true")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Hide per-client and per-service-device blocks; show only groups/suspects/flags.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Force full per-client and per-service-device blocks, overriding auto-compact.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    if args.compact and args.full:
        parser.error("Use either --compact or --full, not both.")
    if args.target:
        if args.hostnames or args.urls:
            parser.error("Use either the positional target or --hostname/--url, not both.")
        if "://" in args.target:
            args.urls = [args.target]
        else:
            args.hostnames = [args.target]

    KatharaAPI = _load_api_class()
    api = KatharaAPI(lab_name=args.lab)
    all_hosts = _resolve_devices(api, [], ["hosts"], expand_neighbors=False)
    default_clients = all_hosts if len(all_hosts) <= AUTO_FULL_CLIENT_MAX else all_hosts[:DEFAULT_CLIENT_SAMPLE]
    service_devices = list(dict.fromkeys(args.service_devices or _default_web_service_devices(api)))
    expected_service_ips = _expected_service_ips(api)
    published_hostnames = _published_hostnames(api, expected_service_ips)
    hostnames = list(dict.fromkeys(args.hostnames))
    if not hostnames and not args.urls and published_hostnames:
        hostnames = list(published_hostnames)
    expected_addresses_by_hostname = _expected_addresses_by_hostname(api, hostnames)
    urls = list(dict.fromkeys(args.urls + [f"http://{hostname}/" for hostname in hostnames]))
    if not hostnames and not args.urls:
        urls = [device_url for device in service_devices if (device_url := _device_http_url(api, device))]
    clients = list(dict.fromkeys(args.clients or default_clients))
    tested_hostnames = {hostname for hostname in hostnames}
    for url in args.urls:
        hostname = _hostname_from_url(url)
        if hostname and not IP_RE.fullmatch(hostname):
            tested_hostnames.add(hostname)
    missing_hostnames = [hostname for hostname in published_hostnames if hostname not in tested_hostnames]
    coverage_warnings: list[str] = []
    if published_hostnames and not tested_hostnames:
        coverage_warnings.append("no_hostname_dns_coverage")
    elif published_hostnames and missing_hostnames:
        coverage_warnings.append(
            f"partial_hostname_coverage:{len(tested_hostnames)}/{len(published_hostnames)}"
        )
    if not args.clients and len(default_clients) < len(all_hosts):
        coverage_warnings.append(f"sampled_clients:{len(default_clients)}/{len(all_hosts)}")

    if not urls and not hostnames:
        parser.error("Provide a target, or ensure the topology exposes web/load_balancer devices.")

    client_records = [
        _client_snapshot(
            api,
            device,
            hostnames,
            urls,
            args.times,
            set(published_hostnames),
            expected_service_ips,
            expected_addresses_by_hostname,
        )
        for device in clients
    ]

    resolver_groups: dict[str, list[str]] = defaultdict(list)
    for client in client_records:
        resolver_groups[client["resolver_key"]].append(client["device"])

    dns_groups: dict[str, dict[str, list[str]]] = {}
    for hostname in hostnames:
        grouped: dict[str, list[str]] = defaultdict(list)
        for client in client_records:
            result = client["dns"].get(hostname)
            if result:
                grouped[_dns_signature(result)].append(client["device"])
        if grouped:
            dns_groups[hostname] = {key: sorted(value) for key, value in sorted(grouped.items())}

    http_groups: dict[str, dict[str, list[str]]] = {}
    for url in urls:
        grouped: dict[str, list[str]] = defaultdict(list)
        for client in client_records:
            result = client["http"].get(url)
            if result:
                grouped[_http_signature(result)].append(client["device"])
        if grouped:
            http_groups[url] = {key: sorted(value) for key, value in sorted(grouped.items())}

    suspect_clients = sorted({client["device"] for client in client_records if client["flags"]})

    auto_compact = len(client_records) > AUTO_COMPACT_CLIENT_THRESHOLD
    if args.full:
        compact = False
    elif args.compact:
        compact = True
    else:
        compact = auto_compact

    suspect_set = set(suspect_clients)
    full_clients = client_records
    # In compact mode, only emit suspect clients in the JSON payload too —
    # the per-client per-URL block on l-size topologies (16+ clients × N
    # URLs) is the main JSON-bloat source for this helper.
    json_clients = full_clients if not compact else [
        c for c in full_clients if c["device"] in suspect_set
    ]
    payload = {
        "lab_name": api.lab.name,
        "hostnames": hostnames,
        "urls": urls,
        "published_hostnames": published_hostnames,
        "missing_hostnames": missing_hostnames,
        "coverage_warnings": coverage_warnings,
        "all_host_count": len(all_hosts),
        "client_sampled": not args.clients and len(default_clients) < len(all_hosts),
        "resolver_groups": {key: sorted(value) for key, value in sorted(resolver_groups.items())},
        "dns_groups": dns_groups,
        "http_groups": http_groups,
        "suspect_clients": suspect_clients,
        "clients": full_clients,  # text summary may need all
        "service_devices": [
            _service_device_snapshot(api, device, args.localhost_url, args.times, include_processes=not args.no_processes)
            for device in service_devices
        ],
        "compact": compact,
        "auto_compact": auto_compact and not args.full,
    }
    if args.as_json:
        # Emit a compact JSON view: drop clean client records when in compact
        # mode so the payload stays parseable.
        json_payload = dict(payload)
        json_payload["clients"] = json_clients
        json_payload["clean_clients_omitted"] = compact and len(json_clients) < len(full_clients)
        print(json.dumps(json_payload, indent=2))
    else:
        print(_text_summary(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
