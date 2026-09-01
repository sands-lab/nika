#!/usr/bin/env python
"""
Best-effort reachability helper for cases where MCP get_reachability() fails.

The MCP tool is all-or-nothing: it first runs `ip -j addr` on every host/server
to learn IPs, and a single non-JSON response aborts the whole tool. This helper
keeps going, records per-device IP lookup failures, and still returns partial
reachability coverage for the devices that answered.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from pathlib import Path
from typing import Any


SRC_ROOT = Path(__file__).resolve().parents[7]
REPO_ROOT = SRC_ROOT.parent
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


def _host_list(api: Any) -> list[str]:
    api.load_machines()
    host_names = list(api.hosts)
    for servers in api.servers.values():
        for server in servers:
            if server not in host_names:
                host_names.append(server)
    return sorted(host_names)


def _safe_host_ip(api: Any, host_name: str) -> tuple[str | None, str | None]:
    try:
        return api.get_host_ip(host_name), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


async def _run_best_effort(api: Any, max_dests: int) -> dict:
    host_names = _host_list(api)

    host_ips: dict[str, str | None] = {}
    ip_lookup_failures: list[dict] = []
    for host_name in host_names:
        ip, error = _safe_host_ip(api, host_name)
        host_ips[host_name] = ip
        if error:
            ip_lookup_failures.append({"host": host_name, "error": error})

    reachable_hosts = sorted([(name, ip) for name, ip in host_ips.items() if ip])
    if max_dests <= 0:
        dst_list = reachable_hosts
    elif len(reachable_hosts) > max_dests:
        rng = random.Random(0)
        dst_list = reachable_hosts.copy()
        rng.shuffle(dst_list)
        dst_list = sorted(dst_list[:max_dests])
    else:
        dst_list = reachable_hosts

    coroutines = []
    pairs = []
    for src_name, _src_ip in reachable_hosts:
        for dst_name, dst_ip in dst_list:
            if src_name == dst_name:
                continue
            pairs.append((src_name, dst_name, dst_ip))
            coroutines.append(api._check_ping_success_async(src_name, dst_ip))

    responses = await asyncio.gather(*coroutines) if coroutines else []

    results = []
    for (src, dst, dst_ip), stats in zip(pairs, responses):
        if stats is None or not isinstance(stats, dict):
            stats = {}
        results.append(
            {
                "src": src,
                "dst": dst,
                "dst_ip": dst_ip,
                "tx": stats.get("tx"),
                "rx": stats.get("rx"),
                "loss_percent": stats.get("loss_percent"),
                "time_ms": stats.get("time_ms"),
                "rtt_avg_ms": stats.get("rtt_avg_ms"),
                "status": stats.get("status"),
            }
        )

    return {
        "hosts": host_ips,
        "ip_lookup_failures": ip_lookup_failures,
        "results": results,
    }


def _summary(payload: dict, max_dests: int) -> str:
    lines = []
    hosts = payload["hosts"]
    failures = payload["ip_lookup_failures"]
    results = payload["results"]
    statuses: dict[str, int] = {}
    for item in results:
        statuses[item["status"]] = statuses.get(item["status"], 0) + 1

    lines.append("=== SAFE REACHABILITY SUMMARY ===")
    lines.append("Mode: best-effort sampled fallback after MCP get_reachability() aborted")
    lines.append(f"Total devices considered: {len(hosts)}")
    lines.append(f"Devices with IPs: {sum(1 for ip in hosts.values() if ip)}")
    lines.append(f"IP lookup failures: {len(failures)}")
    if max_dests <= 0:
        lines.append("Destination sample: all reachable devices")
    else:
        lines.append(f"Destination sample: up to {max_dests} reachable devices")
    if failures:
        lines.append("IP lookup failures:")
        for item in failures:
            lines.append(f"  - {item['host']}: {item['error']}")
    lines.append(f"Ping tests run: {len(results)}")
    if statuses:
        lines.append(
            "Ping status counts: " + ", ".join(f"{status}={count}" for status, count in sorted(statuses.items()))
        )
    else:
        lines.append("Ping status counts: none")
    lines.append("Interpretation:")
    if failures:
        lines.append("  - One or more devices failed IP lookup. Treat those devices as direct investigation targets.")
        lines.append("  - Run pressure_sweep next; if the same running device is `exec_failed`, treat it as resource/load evidence.")
    else:
        lines.append("  - No device failed IP lookup in this fallback run, but this DOES NOT clear the original MCP failure.")
        lines.append("  - The original `get_reachability()` parse failure (e.g. 'Failed to parse `ip -j addr` output')")
        lines.append("    is itself evidence: SOME device's bulk shell output was malformed when MCP queried.")
        lines.append("    Most common cause: the device is CPU-exhausted (resource contention / DoS / overload).")
        lines.append("    Per-host re-queries can succeed when the load eases briefly — that does NOT mean healthy.")
        lines.append("  - Required next step: run pressure_sweep across hosts + servers + routers to find the")
        lines.append("    overloaded device. Do NOT submit `is_anomaly=False` based on safe_reachability alone.")
    lines.append("  - Clean sampled pings do NOT clear the original MCP failure.")
    lines.append("  - This output is partial coverage only; use it to steer the next checks, not to declare the network healthy.")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Best-effort reachability when MCP get_reachability() aborts.")
    parser.add_argument("--lab", default=os.getenv("LAB_NAME", "campus_lan"))
    parser.add_argument("--max-dests", type=int, default=2, help="Number of destination devices to sample.")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    KatharaAPI = _load_api_class()
    api = KatharaAPI(lab_name=args.lab)
    payload = asyncio.run(_run_best_effort(api, max_dests=args.max_dests))
    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(_summary(payload, max_dests=args.max_dests), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
