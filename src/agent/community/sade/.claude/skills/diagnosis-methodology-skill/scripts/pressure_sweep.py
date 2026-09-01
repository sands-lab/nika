#!/usr/bin/env python
"""
Pressure sweep across service-path devices.

Combines `ps aux --sort=-%cpu`, `/proc/loadavg`, and `ss -s` in a single
per-device pass to cover these symptoms:

- `load_balancer_overload`    -> stress/stress-ng on LB, >50% CPU daemon, ESTAB spike
- `web_dos_attack`            -> ab/hey/wrk/hping/httperf on clients, ESTAB/TW spike on server
- `sender_resource_contention`  -> stress on sender host
- `sender_application_delay`    -> sleep/tc netem loopback injector on sender
- `receiver_resource_contention`-> stress on receiver host

One compound exec per device:
    ===PS=== ps aux --sort=-%cpu | head -12
    ===LOAD=== cat /proc/loadavg
    ===SS=== ss -s

Default scope: servers + hosts + routers + switches (everything that can carry a
per-host fault injector). The helper emits a compact per-device verdict; use
`--show-clean` to print unflagged rows as well. This keeps the output tight for
l/m topologies where the service_snapshot dump would otherwise overflow.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any

from network_inventory import _lab_groups, _load_api_class, _resolve_devices


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

# Per-role expected daemons. Order matters: the first hit is enough; if none
# of the alternatives appears in the full ps listing we flag `daemon_absent`.
EXPECTED_DAEMONS: dict[str, tuple[str, ...]] = {
    "dns": ("named", "bind9", "dnsmasq"),
    "dhcp": ("dhcpd", "isc-dhcp-server"),
    "web": ("nginx", "apache2", "httpd", "gunicorn", "uwsgi", "python3"),
    "load_balancer": ("haproxy", "nginx"),
    "database": ("mysqld", "mariadbd", "postgres", "redis-server"),
}

# Process-name fingerprints. First match wins per line.
PRESSURE_FINGERPRINTS: list[tuple[str, re.Pattern[str], str]] = [
    ("stress_tool", re.compile(r"\b(stress-ng|stress)\b"), "synthetic CPU/mem pressure injector"),
    ("http_flood_tool", re.compile(r"\b(ab|hey|wrk|wrk2|httperf|hping3|siege|vegeta|locust)\b"), "HTTP flood / stress client"),
    ("sleep_injector", re.compile(r"\b(sleep|usleep|tc-netem|nanosleep)\b"), "artificial delay injector"),
    ("tc_netem", re.compile(r"\btc\b.*\bnetem\b"), "tc netem loopback delay"),
]

SERVICE_DAEMONS = re.compile(r"\b(nginx|haproxy|apache2|httpd|python3|gunicorn|uwsgi|named|bind9|dnsmasq|dhcpd|isc-dhcp|mysqld|postgres|redis-server)\b")

# Kathara idle ranges (mean baseline observed on unloaded topologies):
#   loadavg 1min < 0.8, cpu hot threshold for daemons > 40%.
# A hot user-space daemon or a stress injector is the signal we care about.
CPU_HOT_THRESHOLD = 50.0
CPU_DAEMON_THRESHOLD = 40.0
LOAD_SPIKE_THRESHOLD = 1.0

# ss -s format:
#   Total: N
#   TCP:   N (estab K, closed L, orphaned M, timewait O)
SS_TOTAL_RE = re.compile(r"^\s*Total:\s*(\d+)", re.MULTILINE)
SS_TCP_ESTAB_RE = re.compile(r"estab\s+(\d+)")
SS_TCP_TW_RE = re.compile(r"timewait\s+(\d+)")
SS_TCP_RE = re.compile(r"^\s*TCP:\s*(\d+)\s*\(.*?\)", re.MULTILINE)

# Socket thresholds tuned for Kathara idle (<20 sockets typical on a router).
TCP_ESTAB_THRESHOLD = 80
TCP_TW_THRESHOLD = 200
SOCK_TOTAL_THRESHOLD = 250


def _command_failed(output: str) -> bool:
    return output.startswith("[TIMEOUT]") or output.startswith("Machine ") or "not found in lab" in output


def _safe_exec(api: Any, device: str, command: str) -> tuple[str, str | None]:
    output = api.exec_cmd(device, command)
    if isinstance(output, list):
        output = "\n".join(output)
    if _command_failed(output):
        return output, output
    return output, None


def _split_sections(raw: str) -> dict[str, str]:
    sections: dict[str, str] = {"PS": "", "PSFULL": "", "LOAD": "", "SS": ""}
    current: str | None = None
    buffer: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("===") and stripped.endswith("==="):
            if current is not None:
                sections[current] = "\n".join(buffer).rstrip()
            current = stripped.strip("=").strip()
            buffer = []
            continue
        buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).rstrip()
    return sections


def _parse_ps_rows(raw: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines()[1:]:  # skip header
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        try:
            pcpu = float(parts[2])
            pmem = float(parts[3])
        except ValueError:
            continue
        cmd = parts[10].strip()
        if not cmd:
            continue
        argv0 = cmd.split()[0].rsplit("/", 1)[-1]
        # Filter wrapper/grep/self noise.
        if argv0 in {"ps", "grep", "head", "sh", "bash", "awk", "sed", "tr"}:
            continue
        rows.append(
            {
                "user": parts[0],
                "pid": parts[1],
                "pcpu": pcpu,
                "pmem": pmem,
                "cmd": cmd,
                "argv0": argv0,
            }
        )
    return rows


def _parse_loadavg(raw: str) -> tuple[float | None, float | None, float | None]:
    stripped = raw.strip().split()
    if len(stripped) < 3:
        return None, None, None
    try:
        return float(stripped[0]), float(stripped[1]), float(stripped[2])
    except ValueError:
        return None, None, None


def _parse_ss(raw: str) -> dict[str, int | None]:
    total = None
    estab = None
    timewait = None
    tcp_total = None
    match = SS_TOTAL_RE.search(raw)
    if match:
        total = int(match.group(1))
    match = SS_TCP_ESTAB_RE.search(raw)
    if match:
        estab = int(match.group(1))
    match = SS_TCP_TW_RE.search(raw)
    if match:
        timewait = int(match.group(1))
    match = SS_TCP_RE.search(raw)
    if match:
        tcp_total = int(match.group(1))
    return {"total": total, "estab": estab, "timewait": timewait, "tcp_total": tcp_total}


def _classify_process(row: dict[str, Any]) -> tuple[str, str] | None:
    for name, pattern, human in PRESSURE_FINGERPRINTS:
        if pattern.search(row["cmd"]) or pattern.search(row["argv0"]):
            return name, human
    return None


def _parse_ps_binaries(raw: str) -> set[str]:
    """Return the set of argv[0] basenames seen anywhere in the full ps listing.

    Handles common display variants:
    - `/usr/sbin/nginx -g ...`        -> "nginx"
    - `nginx: master process ...`     -> "nginx" (strip trailing ":")
    - `[kworker/0:0]`                 -> "kworker/0:0" (kept for visibility)
    - `python3 /opt/app/server.py`    -> "python3"
    """
    binaries: set[str] = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        first = stripped.split()[0]
        basename = first.rsplit("/", 1)[-1]
        # "nginx:" (master/worker process) -> "nginx". Same for any trailing ":".
        basename = basename.rstrip(":")
        # Drop obvious shell wrappers and self-probe noise.
        if basename in {"ps", "grep", "sh", "bash", "head", "awk", "sed", "tr", "echo", "cat"}:
            continue
        if basename:
            binaries.add(basename)
    return binaries


def _daemon_absent_flags(role: str | None, ps_full: set[str]) -> list[str]:
    """Flag daemon_absent when a server role has no matching daemon in the full ps list.

    We only emit a single flag per role so a single `daemon_absent:dns` line is
    enough signal for the agent to pivot to `dns-fault-skill`.
    """
    if not role:
        return []
    expected = EXPECTED_DAEMONS.get(role)
    if not expected:
        return []
    if any(name in ps_full for name in expected):
        return []
    return [f"daemon_absent:{role} (expected one of {'|'.join(expected)})"]


def _device_snapshot(api: Any, device: str, top_n: int, role: str | None = None) -> dict[str, Any]:
    command = (
        f"echo '===PS==='; ps aux --sort=-%cpu --no-headers | head -{top_n}; "
        "echo '===PSFULL==='; ps -eo cmd --no-headers; "
        "echo '===LOAD==='; cat /proc/loadavg; "
        "echo '===SS==='; ss -s"
    )
    raw, error = _safe_exec(api, device, command)
    if error:
        return {"device": device, "role": role, "error": error, "flags": ["exec_failed"]}

    sections = _split_sections(raw)
    # ps --no-headers removes the header row -- re-add a fake row so our parser's
    # "skip header" assumption still works.
    ps_raw = "HEADER\n" + sections["PS"]
    rows = _parse_ps_rows(ps_raw)
    ps_full_binaries = _parse_ps_binaries(sections["PSFULL"])
    load1, load5, load15 = _parse_loadavg(sections["LOAD"])
    ss_counts = _parse_ss(sections["SS"])

    pressure_hits: list[dict[str, Any]] = []
    hot_processes: list[dict[str, Any]] = []
    hot_daemons: list[dict[str, Any]] = []
    for row in rows:
        classification = _classify_process(row)
        if classification:
            name, human = classification
            pressure_hits.append(
                {
                    "classification": name,
                    "reason": human,
                    "pcpu": row["pcpu"],
                    "pmem": row["pmem"],
                    "cmd": row["cmd"],
                    "user": row["user"],
                }
            )
        if row["pcpu"] >= CPU_HOT_THRESHOLD:
            hot_processes.append(
                {
                    "pcpu": row["pcpu"],
                    "pmem": row["pmem"],
                    "cmd": row["cmd"],
                    "argv0": row["argv0"],
                }
            )
        elif row["pcpu"] >= CPU_DAEMON_THRESHOLD and SERVICE_DAEMONS.search(row["cmd"]):
            hot_daemons.append(
                {
                    "pcpu": row["pcpu"],
                    "pmem": row["pmem"],
                    "cmd": row["cmd"],
                    "argv0": row["argv0"],
                }
            )

    flags: list[str] = []
    seen_classifications: set[str] = set()
    for hit in pressure_hits:
        if hit["classification"] not in seen_classifications:
            flags.append(hit["classification"])
            seen_classifications.add(hit["classification"])
    # Fingerprint scan on the FULL ps listing, not just top-N by CPU. Stress
    # tools that spawn many worker processes (e.g. `stress-ng --cpu 0`
    # launches one worker per CPU, appearing as `stress-ng-cpu [run]`) can
    # push the master out of the top-12 CPU sample even though the tool is
    # active. Matching against PSFULL avoids that sampling gap.
    for binary in ps_full_binaries:
        for fp_name, fp_pattern, _ in PRESSURE_FINGERPRINTS:
            if fp_pattern.search(binary) and fp_name not in seen_classifications:
                flags.append(fp_name)
                seen_classifications.add(fp_name)
                pressure_hits.append({
                    "classification": fp_name,
                    "reason": "matched in full ps listing (not in top-CPU sample)",
                    "pcpu": None,
                    "pmem": None,
                    "cmd": binary,
                    "user": "?",
                })
                break
    if hot_processes:
        flags.append("cpu_hot_process")
    if hot_daemons and "cpu_hot_process" not in flags:
        flags.append("cpu_hot_service_daemon")
    # NOTE: `/proc/loadavg` inside a Kathara container reflects the Docker
    # HOST's load, not per-container load — every container on the same host
    # reads the same number. A "loadavg_spike" flag therefore fires on every
    # device identically and hides the real per-device signals. We keep the
    # loadavg value in the payload for context but do NOT flag on it.
    estab = ss_counts.get("estab")
    if estab is not None and estab >= TCP_ESTAB_THRESHOLD:
        flags.append("tcp_estab_spike")
    timewait = ss_counts.get("timewait")
    if timewait is not None and timewait >= TCP_TW_THRESHOLD:
        flags.append("tcp_timewait_spike")
    total = ss_counts.get("total")
    if total is not None and total >= SOCK_TOTAL_THRESHOLD:
        flags.append("socket_total_spike")

    flags.extend(_daemon_absent_flags(role, ps_full_binaries))

    return {
        "device": device,
        "role": role,
        "flags": flags,
        "pressure_hits": pressure_hits,
        "hot_processes": hot_processes,
        "hot_daemons": hot_daemons,
        "loadavg": {"1m": load1, "5m": load5, "15m": load15},
        "ss": ss_counts,
        "top_processes": [
            {"pcpu": r["pcpu"], "pmem": r["pmem"], "argv0": r["argv0"]}
            for r in rows[: min(5, len(rows))]
        ],
    }


def _text_summary(payload: dict[str, Any], show_clean: bool) -> str:
    lines: list[str] = []
    displayed_suspects = list(payload["suspect_devices"])
    displayed_suspects.extend(
        f"{device} (exec_failed)" for device in payload["exec_failures"]
    )
    lines.append("=== PRESSURE SWEEP ===")
    lines.append(f"Lab: {payload['lab_name']}")
    lines.append(f"Devices scanned: {payload['device_count']}")
    lines.append(f"Suspect devices: {', '.join(displayed_suspects) if displayed_suspects else 'none'}")
    if payload["exec_failures"]:
        lines.append(f"Exec failures: {', '.join(payload['exec_failures'])}")
        lines.append(
            "Interpretation: isolated exec_failed is resource/load evidence; host_crash means killed/stopped container."
        )
    lines.append("")

    devices = payload["devices"]
    shown = 0
    for entry in devices:
        if not entry["flags"] and not show_clean:
            continue
        shown += 1
        role_tag = f" [role={entry.get('role')}]" if entry.get("role") else ""
        lines.append(f"{entry['device']}{role_tag}")
        if entry.get("error"):
            flags = ", ".join(entry["flags"]) if entry["flags"] else "none"
            lines.append(f"  Flags: {flags}")
            lines.append(f"  Error: {entry['error'].splitlines()[0] if entry['error'] else '(unknown)'}")
            lines.append("")
            continue
        flags = ", ".join(entry["flags"]) if entry["flags"] else "none"
        lines.append(f"  Flags: {flags}")
        load = entry["loadavg"]
        load_text = (
            f"{load['1m']:.2f} / {load['5m']:.2f} / {load['15m']:.2f}"
            if load["1m"] is not None
            else "(unavailable)"
        )
        lines.append(f"  Loadavg (1/5/15): {load_text}")
        ss = entry["ss"]
        ss_text = (
            f"total={ss.get('total')} tcp={ss.get('tcp_total')} "
            f"estab={ss.get('estab')} timewait={ss.get('timewait')}"
        )
        lines.append(f"  Sockets: {ss_text}")
        if entry["pressure_hits"]:
            lines.append("  Pressure processes:")
            for hit in entry["pressure_hits"][:6]:
                cpu_text = f"{hit['pcpu']:.1f}%" if isinstance(hit['pcpu'], (int, float)) else "?"
                mem_text = f"{hit['pmem']:.1f}%" if isinstance(hit['pmem'], (int, float)) else "?"
                lines.append(
                    f"    [{hit['classification']}] cpu={cpu_text} mem={mem_text} "
                    f"user={hit['user']} cmd={hit['cmd'][:80]}"
                )
        if entry["hot_processes"]:
            lines.append("  Hot CPU processes (>= %.0f%%):" % CPU_HOT_THRESHOLD)
            for hot in entry["hot_processes"][:4]:
                lines.append(f"    cpu={hot['pcpu']:.1f}% mem={hot['pmem']:.1f}% cmd={hot['cmd'][:80]}")
        if entry["hot_daemons"] and "cpu_hot_process" not in entry["flags"]:
            lines.append("  Hot service daemons:")
            for hot in entry["hot_daemons"][:4]:
                lines.append(f"    cpu={hot['pcpu']:.1f}% mem={hot['pmem']:.1f}% cmd={hot['cmd'][:80]}")
        if show_clean and not entry["flags"] and entry["top_processes"]:
            lines.append("  Top (quiet):")
            for top in entry["top_processes"]:
                lines.append(f"    cpu={top['pcpu']:.1f}% mem={top['pmem']:.1f}% {top['argv0']}")
        lines.append("")

    if shown == 0:
        lines.append("(all scanned devices clean — no pressure flags)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Pressure / resource sweep across service-path devices.")
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
    parser.add_argument("--top", type=int, default=10, help="How many ps rows to return per device.")
    parser.add_argument("--show-clean", action="store_true", help="Also print devices with no flags.")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    KatharaAPI = _load_api_class()
    api = KatharaAPI(lab_name=args.lab)
    groups = args.groups if args.groups else ([] if args.devices else DEFAULT_GROUPS)
    devices = _resolve_devices(api, args.devices, groups, expand_neighbors=False)
    if not devices:
        parser.error("No devices resolved. Pass --device/--group or run with defaults on a loaded lab.")

    inventory = _lab_groups(api)
    server_role_by_device: dict[str, str] = {}
    for role, members in (inventory.get("servers") or {}).items():
        for member in members:
            server_role_by_device[member] = role

    entries = [
        _device_snapshot(api, device, args.top, role=server_role_by_device.get(device))
        for device in devices
    ]
    suspect_devices = [entry["device"] for entry in entries if entry["flags"] and "exec_failed" not in entry["flags"]]
    exec_failures = [entry["device"] for entry in entries if "exec_failed" in entry["flags"]]
    triage_suspect_devices = suspect_devices + [
        f"{device} (exec_failed)" for device in exec_failures
    ]

    # On l-size topologies the per-device entries explode the JSON payload.
    # Default: emit only suspect entries (those with flags) — the rest are
    # summarized as a count. `--show-clean` brings them all back.
    suspect_entries = [e for e in entries if e["flags"]]
    clean_count = len(entries) - len(suspect_entries)
    payload = {
        "lab_name": api.lab.name,
        "device_count": len(entries),
        "suspect_devices": suspect_devices,
        "exec_failures": exec_failures,
        "triage_suspect_devices": triage_suspect_devices,
        "devices": entries if args.show_clean else suspect_entries,
        "clean_devices_omitted": (not args.show_clean) and clean_count > 0,
        "clean_count": clean_count,
    }
    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(_text_summary(payload, show_clean=args.show_clean), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
