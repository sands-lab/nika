#!/usr/bin/env python
"""
Compact HTTP client snapshot helper.

This catches host-selective HTTP failures that stay invisible when the agent
tests only one healthy endpoint. It scans a selected device set (`hosts` by
default), runs lightweight curl canaries to one hostname or URL, groups devices
by HTTP outcome, and highlights outlier clients. When called without an
explicit target, it prefers live published service hostnames over direct IPs so
the sweep can still surface DNS-driven HTTP breakage.

Primary use cases:
- `http_acl_block`
- host-selective service failure with clean reachability
- proving "most hosts succeed, this host fails" before deeper DNS/LB checks
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from statistics import mean
from typing import Any

from network_inventory import _lab_groups, _load_api_class, _resolve_devices


IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
ZONE_A_RECORD_RE = re.compile(r"^([A-Za-z0-9_-]+|@)\s+IN\s+A\s+((?:\d{1,3}\.){3}\d{1,3})\s*$")
ZONE_BLOCK_RE = re.compile(r'zone\s+"([^"]+)"\s+IN\s*\{.*?file\s+"([^"]+)";', re.IGNORECASE | re.DOTALL)
CURL_FIELD_RE = re.compile(
    r"(http_code|remote_ip|namelookup|connect|appconnect|pretransfer|starttransfer|total):([^,\s]*)"
)


def _command_failed(output: str) -> bool:
    return output.startswith("[TIMEOUT]") or output.startswith("Machine ") or "not found in lab" in output


def _safe_exec(api: Any, device: str, command: str) -> tuple[str, str | None]:
    output = api.exec_cmd(device, command)
    if _command_failed(output):
        return output, output
    return output, None


def _rich_curl_command(url: str, times: int, connect_timeout: int, max_time: int) -> str:
    return (
        f"for i in $(seq 1 {times}); do "
        f"curl -sS --connect-timeout {connect_timeout} --max-time {max_time} "
        f"-o /dev/null "
        f"-w 'http_code:%{{http_code}} remote_ip:%{{remote_ip}} namelookup:%{{time_namelookup}} "
        f"connect:%{{time_connect}} starttransfer:%{{time_starttransfer}} total:%{{time_total}}\\n' "
        f"'{url}' || echo 'curl_failed'; "
        f"done"
    )


def _timing_curl_command(url: str, times: int, connect_timeout: int, max_time: int) -> str:
    return (
        f"for i in $(seq 1 {times}); do "
        f"curl -sS --connect-timeout {connect_timeout} --max-time {max_time} "
        f"-o /dev/null "
        f"-w 'namelookup:%{{time_namelookup}}, connect:%{{time_connect}}, "
        f"appconnect:%{{time_appconnect}}, pretransfer:%{{time_pretransfer}}, "
        f"starttransfer:%{{time_starttransfer}}, total:%{{time_total}}\\n' "
        f"'{url}' || echo 'curl_failed'; "
        f"done"
    )


def _curl_web_test_raw(
    api: Any,
    device: str,
    url: str,
    times: int,
    connect_timeout: int,
    max_time: int,
) -> tuple[str, str | None, str]:
    # Match Kathara's native curl_web_test behavior whenever the caller keeps
    # its default timeouts; otherwise fall back to an equivalent shell probe.
    if connect_timeout == 5 and max_time == 10 and hasattr(api, "curl_web_test"):
        try:
            output = api.curl_web_test(host_name=device, url=url, times=times)
        except Exception:
            output = ""
        if output and not _command_failed(output):
            return output, None, "curl_web_test"
        if output and _command_failed(output):
            return output, output, "curl_web_test"

    raw, error = _safe_exec(api, device, _timing_curl_command(url, times, connect_timeout, max_time))
    return raw, error, "exec_shell"


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
    # Skip bind9 default empty-zone files (db.0, db.127, db.255, db.empty,
    # db.root). They generate bogus hostnames in the inferred-hostname path.
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


def _expected_service_ips(api: Any) -> set[str]:
    addresses: set[str] = set()
    for device in _default_web_service_devices(api):
        if ip := _device_ip(api, device):
            addresses.add(ip)
    return addresses


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


def _published_hostnames(api: Any) -> list[str]:
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
            for label, _ip in _parse_zone_a_records(zone_raw):
                lowered = label.lower()
                if label == "@" or lowered.startswith("ns") or not lowered.startswith("web"):
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
    service_devices = _default_web_service_devices(api)
    ip_by_device = {device: ip for device in service_devices if (ip := _device_ip(api, device))}
    expected: dict[str, str] = {}

    for hostname in hostnames:
        candidates = [device for device in service_devices if _device_matches_hostname(device, hostname)]
        candidates = [device for device in candidates if device in ip_by_device]
        if len(candidates) == 1:
            expected[hostname] = ip_by_device[candidates[0]]

    return expected


def _target_specs(api: Any, hostname: str | None, url: str | None) -> list[dict[str, str | None]]:
    if url:
        url_hostname = _hostname_from_url(url)
        expected_remote_ip = None
        if url_hostname and not IP_RE.fullmatch(url_hostname):
            expected_remote_ip = _expected_addresses_by_hostname(api, [url_hostname]).get(url_hostname)
        return [{"label": url, "url": url, "expected_remote_ip": expected_remote_ip}]
    if hostname:
        expected_by_hostname = _expected_addresses_by_hostname(api, [hostname])
        return [{
            "label": hostname,
            "url": f"http://{hostname}/",
            "expected_remote_ip": expected_by_hostname.get(hostname),
        }]

    published_hostnames = _published_hostnames(api)
    if published_hostnames:
        expected_by_hostname = _expected_addresses_by_hostname(api, published_hostnames)
        return [
            {
                "label": service_hostname,
                "url": f"http://{service_hostname}/",
                "expected_remote_ip": expected_by_hostname.get(service_hostname),
            }
            for service_hostname in published_hostnames
        ]

    specs = []
    for device in _default_web_service_devices(api):
        device_url = _device_http_url(api, device)
        if not device_url:
            continue
        specs.append({"label": device, "url": device_url, "expected_remote_ip": _device_ip(api, device)})
    return specs


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
    connects = [sample["connect"] for sample in samples if isinstance(sample.get("connect"), float)]
    name_lookups = [sample["namelookup"] for sample in samples if isinstance(sample.get("namelookup"), float)]
    starttransfers = [sample["starttransfer"] for sample in samples if isinstance(sample.get("starttransfer"), float)]
    codes = sorted({sample["http_code"] for sample in samples if sample.get("http_code")})
    remote_ips = sorted({sample["remote_ip"] for sample in samples if sample.get("remote_ip")})
    return {
        "samples": samples,
        "errors": errors,
        "http_codes": codes,
        "remote_ips": remote_ips,
        "avg_namelookup": round(mean(name_lookups), 3) if name_lookups else None,
        "avg_connect": round(mean(connects), 3) if connects else None,
        "avg_starttransfer": round(mean(starttransfers), 3) if starttransfers else None,
        "avg_total": round(mean(totals), 3) if totals else None,
    }


def _http_signature(summary: dict[str, Any]) -> str:
    code_text = ",".join(summary["http_codes"]) if summary["http_codes"] else "(none)"
    if not summary["samples"]:
        return "http_problem:no_samples"
    if any(code == "000" or not code.startswith(("2", "3")) for code in summary["http_codes"]):
        return f"http_non_ok:{code_text}"
    if summary["errors"]:
        return f"http_problem:{code_text}"
    return f"http_ok:{code_text}"


def _client_snapshot(
    api: Any,
    device: str,
    url: str,
    expected_remote_ip: str | None,
    expected_service_ips: set[str],
    times: int,
    connect_timeout: int,
    max_time: int,
) -> dict[str, Any]:
    timing_raw, timing_error, probe_method = _curl_web_test_raw(
        api, device, url, times, connect_timeout, max_time
    )
    timing_summary = _http_summary(timing_raw) if not timing_error else {
        "samples": [],
        "errors": [timing_error],
        "http_codes": [],
        "remote_ips": [],
        "avg_namelookup": None,
        "avg_connect": None,
        "avg_starttransfer": None,
        "avg_total": None,
    }
    identity_raw, identity_error = _safe_exec(api, device, _rich_curl_command(url, 1, connect_timeout, max_time))
    identity_summary = _http_summary(identity_raw) if not identity_error else {
        "samples": [],
        "errors": [identity_error],
        "http_codes": [],
        "remote_ips": [],
        "avg_namelookup": None,
        "avg_connect": None,
        "avg_starttransfer": None,
        "avg_total": None,
    }
    summary = {
        **timing_summary,
        "http_codes": identity_summary["http_codes"],
        "remote_ips": identity_summary["remote_ips"],
        "identity_errors": identity_summary["errors"],
    }
    flags: list[str] = []
    if timing_error or not summary["samples"]:
        flags.append("http_problem")
    elif summary["http_codes"] and any(code == "000" or not code.startswith(("2", "3")) for code in summary["http_codes"]):
        flags.append("http_non_ok")
    elif identity_error:
        flags.append("http_identity_probe_failed")
    if expected_remote_ip and summary["remote_ips"] and any(ip != expected_remote_ip for ip in summary["remote_ips"]):
        flags.append("wrong_remote_ip")
    elif not expected_remote_ip and summary["remote_ips"] and expected_service_ips and any(ip not in expected_service_ips for ip in summary["remote_ips"]):
        flags.append("unexpected_service_remote_ip")
    return {
        "device": device,
        "url": url,
        "expected_remote_ip": expected_remote_ip,
        "probe_method": probe_method,
        "signature": _http_signature(summary),
        "flags": flags,
        **summary,
    }


def _text_summary(payload: dict[str, Any], healthy_preview: int) -> str:
    lines = []
    lines.append("=== HTTP CLIENT SNAPSHOT ===")
    lines.append(f"Lab: {payload['lab_name']}")
    lines.append(f"Devices scanned: {len(payload['devices_scanned'])}")
    lines.append(f"Targets scanned: {len(payload['targets'])}")
    lines.append("")

    for index, target in enumerate(payload["targets"]):
        lines.append(f"Target: {target['label']}")
        lines.append(f"URL: {target['url']}")
        if target["probe_method"]:
            lines.append(f"Timing sweep: {target['probe_method']}")
        if target["expected_remote_ip"]:
            lines.append(f"Expected remote IP: {target['expected_remote_ip']}")
        if target["expected_ok_signature"]:
            lines.append(
                f"Expected healthy profile: {target['expected_ok_signature']} "
                f"({target['expected_ok_count']}/{len(payload['devices_scanned'])} devices)"
            )
        lines.append("")

        # Cross-client outcome grouping is produced by service_snapshot's
        # triage view; this script does not re-emit it. Our unique contribution
        # is the rich per-host timing (avg_name/avg_connect/avg_ttfb below)
        # and the expected-healthy-profile majority inference above.

        if target["suspect_hosts"]:
            lines.append("")
            lines.append("Suspect hosts:")
            for record in target["records"]:
                if record["device"] not in target["suspect_hosts"]:
                    continue
                code_text = ", ".join(record["http_codes"]) if record["http_codes"] else "(none)"
                remote_text = ", ".join(record["remote_ips"]) if record["remote_ips"] else "(none)"
                name_text = f"{record['avg_namelookup']:.3f}s" if isinstance(record["avg_namelookup"], float) else "(none)"
                connect_text = f"{record['avg_connect']:.3f}s" if isinstance(record["avg_connect"], float) else "(none)"
                starttransfer_text = (
                    f"{record['avg_starttransfer']:.3f}s"
                    if isinstance(record["avg_starttransfer"], float)
                    else "(none)"
                )
                avg_text = f"{record['avg_total']:.3f}s" if isinstance(record["avg_total"], float) else "(none)"
                flags_text = ", ".join(record["flags"]) if record["flags"] else "none"
                error_text = "; ".join((record["errors"] + record["identity_errors"])[:2]) if (record["errors"] or record["identity_errors"]) else "(none)"
                lines.append(
                    f"- {record['device']}: codes={code_text} remote_ips={remote_text} "
                    f"avg_name={name_text} avg_connect={connect_text} avg_ttfb={starttransfer_text} avg_total={avg_text} "
                    f"flags={flags_text} errors={error_text}"
                )
        else:
            lines.append("")
            lines.append("Suspect hosts: none")

        if target["expected_ok_signature"]:
            healthy_hosts = target["outcome_groups"].get(target["expected_ok_signature"], [])
            if healthy_hosts:
                lines.append("")
                lines.append("Healthy sample:")
                for device in healthy_hosts[:healthy_preview]:
                    lines.append(f"- {device}")

        if index != len(payload["targets"]) - 1:
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Compact HTTP client snapshot helper.")
    parser.add_argument("--lab", default=os.getenv("LAB_NAME", "campus_lan"))
    parser.add_argument("target", nargs="?", help="Optional hostname or full URL shortcut.")
    parser.add_argument("--device", action="append", default=[], dest="devices")
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        choices=["hosts", "routers", "switches", "servers", "bmv2_switches", "ovs_switches", "sdn_controllers", "all"],
        dest="groups",
    )
    parser.add_argument("--hostname")
    parser.add_argument("--url")
    parser.add_argument("--times", type=int, default=2)
    parser.add_argument("--connect-timeout", type=int, default=5)
    parser.add_argument("--max-time", type=int, default=10)
    parser.add_argument("--healthy-preview", type=int, default=6)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()
    if args.target:
        if args.hostname or args.url:
            parser.error("Use either the positional target or --hostname/--url, not both.")
        if "://" in args.target:
            args.url = args.target
        else:
            args.hostname = args.target

    KatharaAPI = _load_api_class()
    api = KatharaAPI(lab_name=args.lab)
    target_specs = _target_specs(api, args.hostname, args.url)
    if not target_specs:
        parser.error("Provide a hostname or URL, or ensure the topology exposes web/load_balancer devices.")
    devices = _resolve_devices(api, args.devices, args.groups or ["hosts"], expand_neighbors=False)
    if not devices:
        parser.error("No client devices resolved for the requested scope.")
    expected_service_ips = _expected_service_ips(api)

    target_payloads = []
    for spec in target_specs:
        records = [
            _client_snapshot(
                api,
                device,
                str(spec["url"]),
                spec.get("expected_remote_ip"),
                expected_service_ips,
                args.times,
                args.connect_timeout,
                args.max_time,
            )
            for device in devices
        ]

        expected_ok_signature = None
        expected_ok_count = 0
        ok_counter = Counter(record["signature"] for record in records if record["signature"].startswith("http_ok:"))
        if ok_counter:
            expected_ok_signature, expected_ok_count = ok_counter.most_common(1)[0]

        suspect_hosts: list[str] = []
        for record in records:
            if record["flags"]:
                suspect_hosts.append(record["device"])
                continue
            if expected_ok_signature and record["signature"] != expected_ok_signature:
                record["flags"].append("different_http_profile")
                suspect_hosts.append(record["device"])

        outcome_groups: dict[str, list[str]] = defaultdict(list)
        for record in records:
            outcome_groups[record["signature"]].append(record["device"])

        target_payloads.append(
            {
                "label": spec["label"],
                "url": spec["url"],
                "probe_method": records[0]["probe_method"] if records else None,
                "expected_remote_ip": spec.get("expected_remote_ip"),
                "expected_ok_signature": expected_ok_signature,
                "expected_ok_count": expected_ok_count,
                "suspect_hosts": sorted(set(suspect_hosts)),
                "outcome_groups": {key: sorted(value) for key, value in sorted(outcome_groups.items())},
                "records": records,
            }
        )

    payload = {
        "lab_name": api.lab.name,
        "devices_scanned": devices,
        "targets": target_payloads,
    }

    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(_text_summary(payload, healthy_preview=max(args.healthy_preview, 0)), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
