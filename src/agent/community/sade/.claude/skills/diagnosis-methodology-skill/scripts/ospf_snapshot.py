#!/usr/bin/env python
"""
One-pass OSPF coverage helper for diagnosis methodology and the OSPF fault skill.

This gathers high-signal OSPF data across all discovered routers without
changing the Kathara MCP server surface:
- FRR process status
- OSPF network statements / areas
- OSPF neighbor state summary
- OSPF route count

The helper intentionally uses single-quoted `vtysh -c '...'` commands because
the underlying `exec_cmd()` wrapper escapes double quotes in a way that can
silently corrupt FRR commands.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


SRC_ROOT = Path(__file__).resolve().parents[7]
REPO_ROOT = SRC_ROOT.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


NETWORK_RE = re.compile(r"^\s*network\s+(\S+)\s+area\s+(\S+)\s*$")


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


FRR_DAEMONS = ("zebra", "ospfd", "watchfrr")


def _parse_ps_daemons(raw: str) -> dict[str, bool]:
    """Parse `ps aux` output and detect FRR daemons by actual command, not by line-wide substring.

    The previous `"zebra" in raw` check was fragile: the `ps aux | grep -E '...'` wrapper
    itself appears in the output, and the literal pattern 'zebra|ospfd|watchfrr' inside a
    bash -c line can match all three even when no daemon is running. We instead inspect the
    final command column of each ps row and reject lines whose command is grep/bash/sh
    running our own probe.
    """
    found = {name: False for name in FRR_DAEMONS}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 10)
        if len(parts) < 11:
            continue
        command = parts[10]
        # Drop the ps/grep/bash wrapper lines that run the probe itself.
        command_head = command.split()[0].rsplit("/", 1)[-1] if command else ""
        if command_head in {"grep", "bash", "sh", "ps"}:
            continue
        # Extract the executable name from the command (argv[0]); compare against daemons.
        binary = command.split()[0].rsplit("/", 1)[-1] if command else ""
        for daemon in FRR_DAEMONS:
            if binary == daemon or binary.startswith(f"{daemon}-") or binary.startswith(f"{daemon}.") or binary == daemon + "d":
                found[daemon] = True
    return found


def _frr_sockets(api: Any, router: str) -> dict[str, Any]:
    """Check the FRR control sockets and the frr systemd service as independent signals."""
    raw, error = _run(
        api,
        router,
        "ls -1 /var/run/frr/*.vty /var/run/frr/*.pid 2>/dev/null; "
        "echo '---'; "
        "systemctl is-active frr 2>/dev/null || true; "
        "echo '---'; "
        "vtysh -c 'show version' 2>&1 | head -3 || true",
    )
    sections = raw.split("---")
    listing = sections[0] if len(sections) > 0 else ""
    systemd_state = sections[1].strip() if len(sections) > 1 else ""
    vtysh_probe = sections[2].strip() if len(sections) > 2 else ""

    sockets = {
        "zebra_vty": "zebra.vty" in listing,
        "ospfd_vty": "ospfd.vty" in listing,
        "watchfrr_pid": "watchfrr.pid" in listing,
    }
    systemd_active = systemd_state.splitlines()[0].strip().lower() if systemd_state else ""
    vtysh_ok = "FRRouting" in vtysh_probe or "Welcome" in vtysh_probe
    vtysh_dead = "failed to connect" in vtysh_probe.lower() or "no such file" in vtysh_probe.lower()
    return {
        "error": error,
        "sockets": sockets,
        "systemd_state": systemd_active,
        "vtysh_probe": vtysh_probe,
        "vtysh_ok": vtysh_ok,
        "vtysh_dead": vtysh_dead,
    }


def _process_status(api: Any, router: str) -> dict[str, Any]:
    ps_raw, ps_error = _run(api, router, "ps -eo pid,user,pcpu,pmem,cmd --sort=-pcpu")
    daemons = _parse_ps_daemons(ps_raw) if not ps_error else {name: False for name in FRR_DAEMONS}
    socket_info = _frr_sockets(api, router)

    # Health model: on Kathara, `ps -eo` does NOT always show `watchfrr` and
    # `zebra` even when they are running normally (they can be invoked in a way
    # that hides them from the standard process listing). Conversely, the vtysh
    # control socket and the zebra/ospfd unix sockets ARE reliable signals: if
    # vtysh answers `show version` and the sockets exist, FRR is up; if vtysh
    # fails to connect and the sockets are missing, FRR is down.
    #
    # We therefore treat socket/vtysh as authoritative and demote ps to a
    # tie-breaker. `systemd_state == active` is not reliable on Kathara either
    # (systemd is usually inactive inside the containers), so we ignore it
    # unless it actively contradicts the other signals.
    signal_vtysh = socket_info["vtysh_ok"] and not socket_info["vtysh_dead"]
    signal_sockets = socket_info["sockets"]["zebra_vty"] and socket_info["sockets"]["ospfd_vty"]
    signal_ps = all(daemons.values())

    # Authoritative: sockets + vtysh. If both indicate alive, the router is
    # healthy regardless of what ps shows. Only when BOTH sockets AND vtysh
    # fail do we call the router unhealthy.
    healthy = (not socket_info["error"]) and signal_vtysh and signal_sockets
    return {
        "router": router,
        "raw": ps_raw,
        "error": ps_error,
        "zebra": daemons["zebra"],
        "ospfd": daemons["ospfd"],
        "watchfrr": daemons["watchfrr"],
        "systemd_state": socket_info["systemd_state"],
        "sockets": socket_info["sockets"],
        "vtysh_ok": socket_info["vtysh_ok"],
        "vtysh_dead": socket_info["vtysh_dead"],
        "signals": {
            "ps": signal_ps,
            "sockets": signal_sockets,
            "vtysh": signal_vtysh,
        },
        "healthy": healthy,
    }


def _extract_ospf_networks(api: Any, router: str) -> dict[str, Any]:
    raw, error = _run(api, router, "vtysh -c 'show running-config'")
    records: list[dict[str, str]] = []
    in_ospf = False
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped == "router ospf":
            in_ospf = True
            continue
        if in_ospf and stripped in {"exit", "!"}:
            if stripped == "!":
                break
            continue
        if not in_ospf:
            continue
        match = NETWORK_RE.match(line)
        if match:
            records.append({"network": match.group(1), "area": _normalize_area(match.group(2))})

    return {
        "raw": raw,
        "error": error,
        "records": records,
    }


def _parse_neighbors(api: Any, router: str) -> dict[str, Any]:
    raw, error = _run(api, router, "vtysh -c 'show ip ospf neighbor'")
    neighbors = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("Neighbor ID"):
            continue
        parts = stripped.split()
        if len(parts) < 7:
            continue
        neighbors.append(
            {
                "neighbor_id": parts[0],
                "state": parts[2],
                "address": parts[5],
                "interface": parts[6],
            }
        )

    states = Counter(entry["state"] for entry in neighbors)
    return {
        "raw": raw,
        "error": error,
        "count": len(neighbors),
        "all_full": bool(neighbors) and all(state.startswith("Full") for state in states),
        "states": dict(states),
        "neighbors": neighbors,
    }


AREA_HEADER_RE = re.compile(r"^\s*Area ID:\s*(\S+)")
AREA_IFACE_RE = re.compile(
    r"Number of interfaces in this area:\s*Total:\s*(\d+),\s*Active:\s*(\d+)"
)
AREA_ADJ_RE = re.compile(r"Number of fully adjacent neighbors in this area:\s*(\d+)")


def _normalize_area(area: str) -> str:
    """Canonicalize OSPF area ID to dotted-decimal form (A.B.C.D).

    FRR emits areas in two surface forms:
      - `show running-config` network statements: plain decimal ('area 1', 'area 66')
      - `show ip ospf` / `show ip ospf interface`: dotted-decimal ('Area ID: 0.0.0.1')

    Every area is a 32-bit identifier internally. We canonicalize to dotted
    form so the snapshot matches what FRR prints in `show ip ospf` and so
    small areas are unambiguous (1 vs 0.0.0.1 both collapse to 0.0.0.1
    instead of the ambiguous short form).
    """
    if not area:
        return area
    stripped = area.strip()
    parts = stripped.split(".")
    if len(parts) == 4:
        try:
            octets = [int(p) for p in parts]
        except ValueError:
            return stripped
        if all(0 <= o <= 255 for o in octets):
            return f"{octets[0]}.{octets[1]}.{octets[2]}.{octets[3]}"
        return stripped
    try:
        n = int(stripped)
    except ValueError:
        return stripped
    if 0 <= n <= 0xFFFFFFFF:
        return f"{(n >> 24) & 0xFF}.{(n >> 16) & 0xFF}.{(n >> 8) & 0xFF}.{n & 0xFF}"
    return stripped


def _parse_show_ip_ospf(raw: str) -> list[dict[str, Any]]:
    """Parse `vtysh -c 'show ip ospf'` into per-area active-participation records.

    Each record has:
      - `area`: normalized area ID (e.g. "0", "1", "66")
      - `interfaces_total`, `interfaces_active`
      - `full_adjacencies`: number of fully adjacent neighbors in this area

    This is the authoritative view of what areas a router is ACTIVELY running,
    independent of `network X area Y` statement parsing. It is the signal that
    reveals ospf_area_misconfiguration: if a router has an area with `>=1`
    active interface but `0` full adjacencies, that area is broken (typically
    because its peer is in a different area).
    """
    areas: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for line in raw.splitlines():
        header = AREA_HEADER_RE.match(line)
        if header:
            if current is not None:
                areas.append(current)
            current = {
                "area": _normalize_area(header.group(1)),
                "area_raw": header.group(1),
                "interfaces_total": None,
                "interfaces_active": None,
                "full_adjacencies": None,
            }
            continue
        if current is None:
            continue
        iface = AREA_IFACE_RE.search(line)
        if iface:
            current["interfaces_total"] = int(iface.group(1))
            current["interfaces_active"] = int(iface.group(2))
            continue
        adj = AREA_ADJ_RE.search(line)
        if adj:
            current["full_adjacencies"] = int(adj.group(1))
            continue
    if current is not None:
        areas.append(current)
    return areas


def _extract_active_areas(api: Any, router: str) -> dict[str, Any]:
    raw, error = _run(api, router, "vtysh -c 'show ip ospf'")
    records = _parse_show_ip_ospf(raw) if not error else []
    return {"raw": raw, "error": error, "records": records}


# `show ip ospf interface` gives per-interface state. Each interface block:
#   eth2 is up
#     ...
#     Internet Address 172.16.0.6/31, Area 0.0.0.1
#     ...
#     State Point-To-Point, Priority 1
#     ...
#     Timer intervals configured, Hello 10s, Dead 40s, Wait 40s, Retransmit 5
#     Neighbor Count is 1, Adjacent neighbor count is 1
IFACE_HEADER_RE = re.compile(r"^(\S+)\s+is\s+(up|down|detached)\b", re.IGNORECASE)
IFACE_AREA_RE = re.compile(r"Area\s+(\S+)", re.IGNORECASE)
IFACE_ADDR_RE = re.compile(r"Internet Address\s+([\d.]+/\d+)")
IFACE_STATE_RE = re.compile(r"State\s+([A-Za-z0-9_-]+)")
IFACE_NEIGH_RE = re.compile(
    r"Neighbor Count is\s+(\d+),\s*Adjacent neighbor count is\s+(\d+)"
)
IFACE_HELLO_RE = re.compile(r"Hello\s+(\d+)s,\s*Dead\s+(\d+)s")


def _parse_show_ip_ospf_interface(raw: str) -> list[dict[str, Any]]:
    """Parse per-interface OSPF state. Each record has:
      ifname, up, area (normalized), state, neighbor_count, adjacent_count,
      hello_interval, dead_interval.
    """
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in raw.splitlines():
        header = IFACE_HEADER_RE.match(line.strip())
        if header:
            if current is not None:
                records.append(current)
            current = {
                "ifname": header.group(1),
                "up": header.group(2).lower() == "up",
                "area": None,
                "address": None,
                "state": None,
                "neighbor_count": None,
                "adjacent_count": None,
                "hello_interval": None,
                "dead_interval": None,
            }
            continue
        if current is None:
            continue
        m_area = IFACE_AREA_RE.search(line)
        if m_area and current["area"] is None:
            current["area"] = _normalize_area(m_area.group(1).rstrip(","))
        m_addr = IFACE_ADDR_RE.search(line)
        if m_addr and current["address"] is None:
            current["address"] = m_addr.group(1)
        if m_area or m_addr:
            continue
        m = IFACE_STATE_RE.search(line)
        if m and current["state"] is None:
            current["state"] = m.group(1)
            continue
        m = IFACE_NEIGH_RE.search(line)
        if m:
            current["neighbor_count"] = int(m.group(1))
            current["adjacent_count"] = int(m.group(2))
            continue
        m = IFACE_HELLO_RE.search(line)
        if m:
            current["hello_interval"] = int(m.group(1))
            current["dead_interval"] = int(m.group(2))
            continue
    if current is not None:
        records.append(current)
    return records


def _extract_active_interfaces(api: Any, router: str) -> dict[str, Any]:
    raw, error = _run(api, router, "vtysh -c 'show ip ospf interface'")
    records = _parse_show_ip_ospf_interface(raw) if not error else []
    return {"raw": raw, "error": error, "records": records}


def _count_ospf_routes(api: Any, router: str) -> dict[str, Any]:
    raw, error = _run(api, router, "vtysh -c 'show ip route ospf'")
    route_lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("Codes:", ">", "C>", "K>", "S>", "C ", "K ", "S ")):
            continue
        if stripped.startswith("O"):
            route_lines.append(stripped)
    return {
        "raw": raw,
        "error": error,
        "count": len(route_lines),
    }


def _router_snapshot(api: Any, router: str) -> dict[str, Any]:
    process = _process_status(api, router)
    networks = _extract_ospf_networks(api, router)
    neighbors = _parse_neighbors(api, router)
    ospf_routes = _count_ospf_routes(api, router)
    active_areas = _extract_active_areas(api, router)
    active_interfaces = _extract_active_interfaces(api, router)
    return {
        "router": router,
        "process": process,
        "ospf_networks": networks["records"],
        "areas": sorted({item["area"] for item in networks["records"]}),
        "active_areas": active_areas["records"],
        "active_interfaces": active_interfaces["records"],
        "neighbors": neighbors,
        "ospf_routes": ospf_routes,
        "errors": {
            "process": process["error"],
            "active_areas": active_areas["error"],
            "active_interfaces": active_interfaces["error"],
            "config": networks["error"],
            "neighbors": neighbors["error"],
            "routes": ospf_routes["error"],
        },
    }


def _flags(snapshot: dict[str, Any]) -> list[str]:
    router = snapshot["router"]
    flags: list[str] = []
    process = snapshot["process"]
    neighbors = snapshot["neighbors"]
    ospf_networks = snapshot["ospf_networks"]
    errors = snapshot["errors"]

    for phase, error in errors.items():
        if error:
            flags.append(f"{router}: {phase} command failed")

    if not process["healthy"]:
        signals = process.get("signals", {})
        sockets = process.get("sockets") or {}
        # Only emit strong "FRR down" signals. In Kathara, `ps` often fails to
        # show watchfrr/zebra even when they're healthy, so do not flag on
        # ps-only absence. The authoritative signals are vtysh and the control
        # sockets — flag when either fails.
        if not signals.get("vtysh", True):
            flags.append(f"{router}: vtysh probe failed (FRR control plane unreachable)")
        missing_sockets = [
            name for name in ("zebra_vty", "ospfd_vty")
            if not sockets.get(name, False)
        ]
        if missing_sockets:
            flags.append(
                f"{router}: FRR control sockets missing ({', '.join(missing_sockets)}) — daemon likely down"
            )
    if not ospf_networks and not errors["config"]:
        flags.append(f"{router}: no OSPF network statements")
    if neighbors["count"] == 0 and not errors["neighbors"]:
        flags.append(f"{router}: zero OSPF neighbors")
    elif not neighbors["all_full"] and not errors["neighbors"]:
        flags.append(f"{router}: non-Full neighbors present: {neighbors['states']}")

    # Per-area adjacency failure: if an area has >=1 active interface but 0
    # full adjacencies, the adjacency process has broken on every link in that
    # area. The classic cause is one side of the link advertising a different
    # area ID — so this is the symptom-level signal the agent should key on.
    for entry in snapshot.get("active_areas", []) or []:
        adjacencies = entry.get("full_adjacencies")
        active = entry.get("interfaces_active")
        if adjacencies == 0 and active and active > 0:
            flags.append(
                f"{router}: area {entry['area']} has 0 full adjacencies across "
                f"{active} active interface(s) — peer mismatch suspected"
            )

    # Per-INTERFACE checks (catches partial failures the per-area aggregate
    # hides — e.g. one uplink broken while a sibling host-facing interface is
    # normally adj=0).
    network_records = snapshot.get("ospf_networks") or []
    network_areas_by_prefix = {(r.get("network") or "").strip(): r.get("area") for r in network_records}
    for iface in snapshot.get("active_interfaces", []) or []:
        ifname = iface.get("ifname", "?")
        # Interface declared down at the OSPF layer.
        if iface.get("up") is False:
            flags.append(f"{router}: OSPF interface {ifname} is down")
        # Interface has neighbors but none reached Full state.
        nbr = iface.get("neighbor_count")
        adj = iface.get("adjacent_count")
        if nbr is not None and adj is not None and nbr > 0 and adj < nbr:
            flags.append(
                f"{router}: OSPF interface {ifname} has {nbr} neighbor(s) but only {adj} adjacent "
                f"(state={iface.get('state')}, area={iface.get('area')}) — peer not reaching Full"
            )
        # Interface running in /31 router-link subnet but no neighbor at all
        # (broken uplink, classic ospf_neighbor_missing fingerprint).
        addr = iface.get("address") or ""
        if addr.endswith("/31") and nbr == 0:
            flags.append(
                f"{router}: OSPF interface {ifname} on point-to-point link {addr} has 0 neighbors "
                f"(area={iface.get('area')}) — adjacency never formed"
            )
        # Interface-level area override that contradicts the router's network
        # statement covering the same prefix (sneaky `ip ospf area X` injection).
        # Match by IP containment (not string prefix) so /31 links inside the
        # same /24 are not cross-matched against each other's network stanzas.
        if addr and iface.get("area") is not None:
            try:
                iface_ip = ipaddress.ip_interface(addr).ip
            except ValueError:
                iface_ip = None
            if iface_ip is not None:
                best: tuple[int, str, Any] | None = None
                for prefix, stmt_area in network_areas_by_prefix.items():
                    if not prefix or "/" not in prefix:
                        continue
                    try:
                        net = ipaddress.ip_network(prefix, strict=False)
                    except ValueError:
                        continue
                    if iface_ip in net:
                        # Longest-prefix wins — the most specific network
                        # statement is the one that covers this interface.
                        if best is None or net.prefixlen > best[0]:
                            best = (net.prefixlen, prefix, stmt_area)
                if best is not None:
                    _, _, stmt_area = best
                    if stmt_area is not None and stmt_area != iface.get("area"):
                        flags.append(
                            f"{router}: OSPF interface {ifname} ({addr}) is running in area "
                            f"{iface.get('area')} but the matching network statement declares area "
                            f"{stmt_area} — interface-level area override"
                        )
    return flags


def _area_consistency_flags(snapshots: list[dict[str, Any]]) -> list[str]:
    """Cross-router OSPF area consistency check.

    Per-router snapshots already flag "zero neighbors" and "non-Full" when an
    adjacency cannot form. But ospf_area_misconfiguration is the sneaky case
    where a router still has most of its neighbors Full — only the one
    interface whose area was changed to a wrong value is silent. In that case
    the per-router view is locally clean and the fault is only visible when
    you cross-reference multiple routers' `network X/Y area Z` statements.

    For every network prefix appearing in any router's OSPF config, group by
    the declared area. If the same prefix is declared under >1 area across
    the fleet, that is a direct area mismatch and is surfaced as a flag.

    Also surfaces area-set asymmetry: a router whose advertised area set is a
    strict subset / superset of its directly-adjacent Full neighbors' areas.
    (This is a weaker signal and is labeled `area_set_divergence`.)
    """
    by_network: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    router_areas: dict[str, set[str]] = {}
    for snapshot in snapshots:
        router = snapshot["router"]
        router_areas[router] = set(snapshot.get("areas") or [])
        for record in snapshot.get("ospf_networks") or []:
            network = record.get("network")
            area = record.get("area")
            if not network or area is None:
                continue
            by_network[network][area].append(router)

    flags: list[str] = []
    for network, area_groups in sorted(by_network.items()):
        if len(area_groups) < 2:
            continue
        parts: list[str] = []
        for area, routers_for_area in sorted(area_groups.items()):
            parts.append(f"area {area}: {', '.join(sorted(set(routers_for_area)))}")
        flags.append(
            f"ospf_area_mismatch: network {network} declared under multiple areas -> "
            + " | ".join(parts)
        )

    # Area-set divergence: compare each router's area set with the union of
    # areas reported by its Full neighbors. A router that advertises an area
    # no neighbor agrees on, or refuses an area every neighbor has, is
    # suspicious even when no neighbor is missing.
    neighbor_areas_by_router: dict[str, set[str]] = defaultdict(set)
    for snapshot in snapshots:
        router = snapshot["router"]
        local_areas = router_areas.get(router, set())
        for entry in snapshot.get("neighbors", {}).get("neighbors", []):
            state = entry.get("state") or ""
            if not state.startswith("Full"):
                continue
            neighbor_id = entry.get("neighbor_id")
            if not neighbor_id:
                continue
            # neighbor_id is typically a router-id IP, not a hostname; match
            # against any router whose advertised areas intersect this router.
            for other in router_areas:
                if other == router:
                    continue
                if router_areas[other] & local_areas:
                    neighbor_areas_by_router[router] |= router_areas[other]

    for router, local in sorted(router_areas.items()):
        if not local:
            continue
        peers = neighbor_areas_by_router.get(router, set())
        if not peers:
            continue
        only_local = local - peers
        if only_local:
            flags.append(
                f"{router}: area_set_divergence: areas {sorted(only_local)} declared locally "
                f"but not seen on any Full peer (peer areas: {sorted(peers)})"
            )

    # Orphaned active area: an area ID that one router is actively running
    # (per `show ip ospf`) but that no other router participates in. This is
    # the direct fingerprint of the sed-based area-misconfig injector — the
    # faulty router ends up in an area like "66" that no peer has. We report
    # it separately from the network-statement cross-check above because the
    # running-process view can diverge from the statement view if the daemon
    # refused or silently reloaded part of the config.
    active_area_to_routers: dict[str, list[str]] = defaultdict(list)
    for snapshot in snapshots:
        router = snapshot["router"]
        for entry in snapshot.get("active_areas", []) or []:
            active_area_to_routers[entry["area"]].append(router)
    for area, routers_for_area in sorted(active_area_to_routers.items()):
        if len(routers_for_area) == 1:
            owner = routers_for_area[0]
            flags.append(
                f"{owner}: orphaned_active_area {area} — active on this router only, no peer shares it"
            )
    return flags


def _text_summary(payload: dict[str, Any]) -> str:
    lines = []
    lines.append("=== OSPF SNAPSHOT ===")
    lines.append(f"Lab: {payload['lab_name']}")
    lines.append(f"Routers scanned: {len(payload['routers'])}")
    if payload["flags"]:
        lines.append("Flags:")
        for flag in payload["flags"]:
            lines.append(f"  - {flag}")
    else:
        lines.append("Flags: none")

    # Coverage summary so the agent can see what was actually inspected, and
    # which per-router data points are worth scanning even when no flag fired.
    routers = payload.get("routers", [])
    healthy_vtysh = sum(1 for r in routers if (r.get("process") or {}).get("vtysh_ok"))
    sockets_ok = sum(
        1 for r in routers
        if (r.get("process") or {}).get("sockets", {}).get("zebra_vty")
        and (r.get("process") or {}).get("sockets", {}).get("ospfd_vty")
    )
    zero_neighbors = sum(1 for r in routers if (r.get("neighbors") or {}).get("count", 0) == 0)
    non_full = sum(1 for r in routers if not (r.get("neighbors") or {}).get("all_full") and (r.get("neighbors") or {}).get("count", 0) > 0)
    partial_adj = []
    for r in routers:
        for area in r.get("active_areas", []) or []:
            adj = area.get("full_adjacencies")
            iface_a = area.get("interfaces_active")
            if adj is not None and iface_a is not None and adj < iface_a:
                partial_adj.append(f"{r['router']}/area-{area['area']}(adj={adj}<iface={iface_a})")
    # Per-interface signals: count broken/suspicious interfaces across all
    # routers. These distinguish "broken uplink" from "host-facing no-peer".
    iface_total = 0
    iface_down = 0
    iface_p2p_no_neighbor = 0
    iface_partial_adj = 0
    for r in routers:
        for iface in r.get("active_interfaces", []) or []:
            iface_total += 1
            if iface.get("up") is False:
                iface_down += 1
            addr = iface.get("address") or ""
            nbr = iface.get("neighbor_count")
            adj = iface.get("adjacent_count")
            if addr.endswith("/31") and nbr == 0:
                iface_p2p_no_neighbor += 1
            if nbr is not None and adj is not None and nbr > 0 and adj < nbr:
                iface_partial_adj += 1
    lines.append("")
    lines.append("Coverage inspected:")
    lines.append(f"  - FRR daemon health (vtysh + control sockets): {healthy_vtysh}/{len(routers)} vtysh ok, {sockets_ok}/{len(routers)} both sockets present")
    lines.append(f"  - OSPF neighbors: {zero_neighbors} routers with zero, {non_full} with non-Full")
    lines.append(f"  - Per-area adj < iface (some interface in the area has 0 adjacencies — normal for host-facing access ports, suspicious on uplinks): {len(partial_adj)}")
    lines.append(f"  - Per-interface state: {iface_total} OSPF interfaces total | down: {iface_down} | point-to-point with 0 neighbors: {iface_p2p_no_neighbor} | with partial adjacency: {iface_partial_adj}")
    if partial_adj:
        for line in partial_adj[:10]:
            lines.append(f"    {line}")
    lines.append("Note: in Kathara, watchfrr=no / zebra=no in the per-router data is baseline noise (kernel-threaded). Authoritative health = vtysh + sockets.")
    lines.append("")
    for item in payload["routers"]:
        proc = item["process"]
        neigh = item["neighbors"]
        errors = item["errors"]
        areas = ", ".join(item["areas"]) if item["areas"] else "(none)"
        networks = ", ".join(f"{entry['network']}@{entry['area']}" for entry in item["ospf_networks"]) or "(none)"
        states = ", ".join(f"{state} x{count}" for state, count in sorted(neigh["states"].items())) or "(none)"
        lines.append(item["router"])
        socket_hint = proc.get("sockets") or {}
        systemd_state = proc.get("systemd_state") or "?"
        vtysh_hint = "ok" if proc.get("vtysh_ok") else ("dead" if proc.get("vtysh_dead") else "?")
        lines.append(
            "  FRR: "
            f"watchfrr={'yes' if proc['watchfrr'] else 'no'}, "
            f"zebra={'yes' if proc['zebra'] else 'no'}, "
            f"ospfd={'yes' if proc['ospfd'] else 'no'}, "
            f"systemd={systemd_state}, "
            f"zebra_vty={'yes' if socket_hint.get('zebra_vty') else 'no'}, "
            f"ospfd_vty={'yes' if socket_hint.get('ospfd_vty') else 'no'}, "
            f"vtysh={vtysh_hint}"
        )
        lines.append(f"  Areas (from config): {areas}")
        lines.append(f"  Networks: {networks}")
        active_areas = item.get("active_areas") or []
        if active_areas:
            area_parts = []
            for entry in active_areas:
                adj = entry.get("full_adjacencies")
                active_ifaces = entry.get("interfaces_active")
                area_parts.append(
                    f"{entry['area']}(adj={adj if adj is not None else '?'},"
                    f"iface={active_ifaces if active_ifaces is not None else '?'})"
                )
            lines.append(f"  Active areas (running): {', '.join(area_parts)}")
        lines.append(f"  Neighbors: {neigh['count']} ({states})")
        lines.append(f"  OSPF routes: {item['ospf_routes']['count']}")
        active_interfaces = item.get("active_interfaces") or []
        if active_interfaces:
            iface_parts = []
            for iface in active_interfaces:
                addr = iface.get("address") or "?"
                ar = iface.get("area") or "?"
                nbr = iface.get("neighbor_count")
                adj = iface.get("adjacent_count")
                state = iface.get("state") or "?"
                iface_parts.append(
                    f"{iface['ifname']}({addr},area={ar},state={state},nbr={nbr},adj={adj})"
                )
            lines.append(f"  OSPF interfaces: {', '.join(iface_parts)}")
        active_errors = [phase for phase, error in errors.items() if error]
        lines.append(f"  Command errors: {', '.join(active_errors) if active_errors else 'none'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Get one-pass OSPF coverage across all discovered routers.")
    parser.add_argument("--lab", default=os.getenv("LAB_NAME", "campus_lan"))
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    KatharaAPI = _load_api_class()
    api = KatharaAPI(lab_name=args.lab)
    routers = _load_routers(api)
    snapshots = [_router_snapshot(api, router) for router in routers]
    if not routers:
        flags = ["No routers discovered. Ensure the lab is running and the correct LAB_NAME is selected."]
    else:
        per_router = [flag for snapshot in snapshots for flag in _flags(snapshot)]
        cross_router = _area_consistency_flags(snapshots)
        flags = per_router + cross_router
    payload = {
        "lab_name": api.lab.name,
        "routers": snapshots,
        "flags": flags,
    }

    if args.as_json:
        # Strip the raw command-output strings before emitting JSON. They're
        # only useful for parser debugging, but each one is 1-3KB; on l-size
        # topologies the sum balloons the payload past the persistence
        # threshold and triggers the oversized-output detour.
        compact = json.loads(json.dumps(payload, default=str))
        for r in compact.get("routers", []):
            for k in ("process", "neighbors", "ospf_routes"):
                if isinstance(r.get(k), dict):
                    r[k].pop("raw", None)
        print(json.dumps(compact, indent=2))
    else:
        print(_text_summary(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
