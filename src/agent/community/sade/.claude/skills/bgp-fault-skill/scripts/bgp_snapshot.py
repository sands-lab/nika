#!/usr/bin/env python
"""
One-pass BGP coverage helper for the BGP fault skill.

This gathers high-signal BGP data across all discovered routers without
changing the Kathara MCP server surface:
- FRR/BGP process status
- BGP ASN and configured network statements
- BGP neighbor state summary
- loopback IPv4 addresses
- static blackhole / Null0 route lines from the running config

Use it as a fast coverage tool before drilling into a specific router.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


SRC_ROOT = Path(__file__).resolve().parents[7]
REPO_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


BGP_RE = re.compile(r"^\s*router bgp\s+(\d+)\s*$")
NETWORK_RE = re.compile(r"^\s*network\s+(\S+)(?:\s+route-map\s+(\S+))?\s*$")
NEIGHBOR_RE = re.compile(r"^\s*neighbor\s+(\S+)\s+remote-as\s+(\d+)\s*$")
IP_ROUTE_NULL_RE = re.compile(r"^\s*ip route\s+(\S+)\s+(Null0|blackhole)\s*$", re.IGNORECASE)
LOOPBACK_IPV4_RE = re.compile(r"\binet\s+(\d+\.\d+\.\d+\.\d+/\d+)\b")


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

# Get routers from the lab API. This assumes that any machine with "router" in its name is a relevant device, which is a common convention in Kathara labs.
def _load_routers(api: Any) -> list[str]:
    api.load_machines()
    return list(api.routers)


def _command_failed(output: str) -> bool:
    return output.startswith("[TIMEOUT]") or output.startswith("Machine ") or "not found in lab" in output


def _run(api: Any, router: str, command: str) -> tuple[str, str | None]:
    raw = api.exec_cmd(router, command)
    if _command_failed(raw):
        return raw, raw
    return raw, None

# The process status check looks for the presence of the main FRR processes (zebra, bgpd, watchfrr) in the process list, which is a strong indicator of whether BGP is likely operational on the router.
def _process_status(api: Any, router: str) -> dict[str, Any]:
    raw, error = _run(api, router, "ps aux | grep -E 'zebra|bgpd|watchfrr' | grep -v grep")
    return {
        "router": router,
        "raw": raw,
        "error": error,
        "zebra": "zebra" in raw,
        "bgpd": "bgpd" in raw,
        "watchfrr": "watchfrr" in raw,
        "healthy": not error and all(name in raw for name in ("zebra", "bgpd", "watchfrr")),
    }

# The BGP config extraction focuses on the ASN, network statements, neighbor statements, and any static null routes, which can indicate misconfigurations or intentional blackholing.
def _extract_bgp_config(api: Any, router: str) -> dict[str, Any]:
    raw, error = _run(api, router, "vtysh -c 'show running-config'")
    asn: str | None = None
    networks: list[dict[str, str]] = []
    neighbors: list[dict[str, str]] = []
    null_routes: list[dict[str, str]] = []
    in_bgp = False

    for line in raw.splitlines():
        stripped = line.strip()

        route_match = IP_ROUTE_NULL_RE.match(line)
        if route_match:
            null_routes.append({"prefix": route_match.group(1), "target": route_match.group(2)})

        bgp_match = BGP_RE.match(line)
        if bgp_match:
            in_bgp = True
            asn = bgp_match.group(1)
            continue

        if not in_bgp:
            continue

        # End of the `router bgp` block: a top-level (column 0) `exit`. The
        # `!` lines inside the block are internal separators (e.g., before
        # `address-family ipv4 unicast`), so they must NOT terminate parsing —
        # otherwise every `network` statement living under address-family is
        # missed and the snapshot reports `Networks: (none)`.
        if line.rstrip() == "exit":
            in_bgp = False
            continue
        if stripped == "!":
            continue

        network_match = NETWORK_RE.match(line)
        if network_match:
            record = {"prefix": network_match.group(1)}
            if network_match.group(2):
                record["route_map"] = network_match.group(2)
            networks.append(record)
            continue

        neighbor_match = NEIGHBOR_RE.match(line)
        if neighbor_match:
            neighbors.append({"neighbor": neighbor_match.group(1), "remote_as": neighbor_match.group(2)})

    return {
        "raw": raw,
        "error": error,
        "asn": asn,
        "networks": networks,
        "configured_neighbors": neighbors,
        "null_routes": null_routes,
    }

# The BGP summary parsing focuses on the neighbor state and prefix counts, which are high-signal indicators of BGP health.
def _parse_bgp_summary(api: Any, router: str) -> dict[str, Any]:
    # Prefer JSON output: text columns shift across FRR versions and the
    # trailing "Desc" field (often "N/A") was being misread as the state.
    raw, error = _run(api, router, "vtysh -c 'show ip bgp summary json'")
    neighbors: list[dict[str, Any]] = []
    if not error:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            data = None
        if isinstance(data, dict):
            af = data.get("ipv4Unicast") or data.get("default", {}).get("ipv4Unicast") or {}
            peers = af.get("peers", {}) if isinstance(af, dict) else {}
            for nbr, info in peers.items():
                if not isinstance(info, dict):
                    continue
                state = info.get("state", "Unknown")
                pfx = info.get("pfxRcd")
                if state == "Established" and not isinstance(pfx, int):
                    pfx = info.get("prefixReceivedCount")
                neighbors.append(
                    {
                        "neighbor": nbr,
                        "remote_as": str(info.get("remoteAs", "")),
                        "state": state,
                        "pfxrcd": pfx if isinstance(pfx, int) else None,
                        "raw_state": state,
                    }
                )

    states = Counter(entry["state"] for entry in neighbors)
    established = sum(1 for entry in neighbors if entry["state"] == "Established")
    return {
        "raw": raw,
        "error": error,
        "count": len(neighbors),
        "established_count": established,
        "states": dict(states),
        "neighbors": neighbors,
    }


def _loopback_ipv4(api: Any, router: str) -> dict[str, Any]:
    raw, error = _run(api, router, "ip addr show lo")
    loopbacks = LOOPBACK_IPV4_RE.findall(raw) if not error else []
    return {
        "raw": raw,
        "error": error,
        "ipv4": loopbacks,
    }


def _router_snapshot(api: Any, router: str) -> dict[str, Any]:
    process = _process_status(api, router)
    config = _extract_bgp_config(api, router)
    summary = _parse_bgp_summary(api, router)
    loopback = _loopback_ipv4(api, router)
    return {
        "router": router,
        "process": process,
        "asn": config["asn"],
        "bgp_networks": config["networks"],
        "configured_neighbors": config["configured_neighbors"],
        "null_routes": config["null_routes"],
        "bgp_summary": summary,
        "loopback_ipv4": loopback["ipv4"],
        "errors": {
            "process": process["error"],
            "config": config["error"],
            "summary": summary["error"],
            "loopback": loopback["error"],
        },
    }


def _flags(snapshot: dict[str, Any]) -> list[str]:
    router = snapshot["router"]
    flags: list[str] = []
    process = snapshot["process"]
    summary = snapshot["bgp_summary"]
    errors = snapshot["errors"]

    for phase, error in errors.items():
        if error:
            flags.append(f"{router}: {phase} command failed")

    if not process["healthy"]:
        missing = [name for name in ("watchfrr", "zebra", "bgpd") if not process[name]]
        if missing:
            flags.append(f"{router}: missing FRR process(es): {', '.join(missing)}")
    if snapshot["null_routes"]:
        prefixes = ", ".join(item["prefix"] for item in snapshot["null_routes"])
        flags.append(f"{router}: static null/blackhole route(s): {prefixes}")
    non_default_lo = [ip for ip in snapshot["loopback_ipv4"] if not ip.startswith("127.")]
    if non_default_lo:
        flags.append(f"{router}: loopback IPv4(s): {', '.join(non_default_lo)}")
    if summary["count"] == 0 and not errors["summary"]:
        flags.append(f"{router}: zero BGP neighbors in summary")
    elif summary["established_count"] < summary["count"] and not errors["summary"]:
        flags.append(f"{router}: non-established BGP neighbors present: {summary['states']}")
    return flags


# The text summary is a human-friendly report of the snapshot data, suitable for quick scanning.
def _text_summary(payload: dict[str, Any]) -> str:
    lines = []
    lines.append("=== BGP SNAPSHOT ===")
    lines.append(f"Lab: {payload['lab_name']}")
    lines.append(f"Routers scanned: {len(payload['routers'])}")
    if payload["flags"]:
        lines.append("Flags:")
        for flag in payload["flags"]:
            lines.append(f"  - {flag}")
    else:
        lines.append("Flags: none")

    lines.append("")
    for item in payload["routers"]:
        proc = item["process"]
        summary = item["bgp_summary"]
        networks = ", ".join(entry["prefix"] for entry in item["bgp_networks"]) or "(none)"
        null_routes = ", ".join(entry["prefix"] for entry in item["null_routes"]) or "(none)"
        loopbacks = ", ".join(item["loopback_ipv4"]) or "(none)"
        states = ", ".join(f"{state} x{count}" for state, count in sorted(summary["states"].items())) or "(none)"
        active_errors = [phase for phase, error in item["errors"].items() if error]
        lines.append(item["router"])
        lines.append(
            "  FRR: "
            f"watchfrr={'yes' if proc['watchfrr'] else 'no'}, "
            f"zebra={'yes' if proc['zebra'] else 'no'}, "
            f"bgpd={'yes' if proc['bgpd'] else 'no'}"
        )
        lines.append(f"  ASN: {item['asn'] or '(none)'}")
        lines.append(f"  Networks: {networks}")
        lines.append(f"  Loopback IPv4: {loopbacks}")
        lines.append(f"  Null routes: {null_routes}")
        lines.append(f"  BGP neighbors: {summary['count']} ({states})")
        lines.append(f"  Command errors: {', '.join(active_errors) if active_errors else 'none'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# The main entry point gathers snapshots for all discovered routers and prints a summary.
def main() -> int:
    parser = argparse.ArgumentParser(description="Get one-pass BGP coverage across all discovered routers.")
    parser.add_argument("--lab", default=os.getenv("LAB_NAME", "campus_lan"))
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    KatharaAPI = _load_api_class()
    api = KatharaAPI(lab_name=args.lab)
    routers = _load_routers(api)
    snapshots = [_router_snapshot(api, router) for router in routers]
    payload = {
        "lab_name": api.lab.name,
        "routers": snapshots,
        "flags": (
            ["No routers discovered. Ensure the lab is running and the correct LAB_NAME is selected."]
            if not routers
            else [flag for snapshot in snapshots for flag in _flags(snapshot)]
        ),
    }

    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(_text_summary(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
