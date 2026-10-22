"""BGP route-level faults seen in remote routers' tables (test-path only).

Hijacks, RPKI-invalid leaks, and missing advertisements change which routes
other routers learn. Each probe reads remote observers' BGP tables (and, for a
hijack, traffic from a host behind the observer). Each ``*_baseline`` records
the healthy reading of the same signal on the problem before inject.
"""

from __future__ import annotations

import ipaddress
import json
import time
from typing import Any

from nika.net_env.net_env_pool import get_probe_path
from nika.net_env.verify import exec_or_empty, ping_stats
from nika.problems.base import build_verify_result
from nika.runtime.base import LabRuntime
from nika.validation.symptom.routing_probes import bgp_speakers_of, host_addresses

# BGP UPDATEs propagate in seconds; the bound covers MRAI and RR reflection.
_ROUTE_WAIT_SEC = 60.0
_POLL_SEC = 3.0


def _json(runtime: LabRuntime, node: str, command: str) -> Any:
    raw = exec_or_empty(runtime, node, command, timeout=30).strip()
    if not raw.startswith("{"):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def prefix_paths(runtime: LabRuntime, router: str, prefix: str) -> list[dict] | None:
    """Paths ``router`` holds for exactly ``prefix``; ``None`` when unreadable."""
    data = _json(runtime, router, f"vtysh -c 'show bgp ipv4 unicast {prefix} json'")
    if data is None:
        return None
    return [
        {
            "aspath": str((path.get("aspath") or {}).get("string") or ""),
            "best": bool((path.get("bestpath") or {}).get("overall")),
            "nexthops": [hop.get("ip") for hop in path.get("nexthops") or []],
            "originator": path.get("originatorId"),
            "peer_router_id": (path.get("peer") or {}).get("routerId"),
        }
        for path in data.get("paths") or []
    ]


def _table(runtime: LabRuntime, router: str) -> dict[str, list[dict]] | None:
    data = _json(runtime, router, "vtysh -c 'show bgp ipv4 unicast json'")
    if data is None or not isinstance(data.get("routes"), dict):
        return None
    return {
        prefix: [
            {
                "aspath": str(path.get("path") or ""),
                "best": bool(path.get("bestpath")),
                "nexthops": [hop.get("ip") for hop in path.get("nexthops") or []],
            }
            for path in paths
        ]
        for prefix, paths in data["routes"].items()
    }


def _local_asn(runtime: LabRuntime, router: str) -> int | None:
    data = _json(runtime, router, "vtysh -c 'show bgp summary json'")
    unicast = (data or {}).get("ipv4Unicast") or {}
    asn = unicast.get("as")
    return int(asn) if isinstance(asn, int) else None


def _origin_asn(path: dict) -> int | None:
    tokens = [t for t in path["aspath"].replace("{", " ").replace("}", " ").split()]
    return int(tokens[-1]) if tokens and tokens[-1].isdigit() else None


def _from_router(
    path: dict, addrs: set[str], asn: int | None, observer_asn: Any
) -> bool:
    """Whether ``path`` was originated by the router owning ``addrs`` / ``asn``.

    Across eBGP the origin AS decides, since a transit hub that rewrites the
    next hop also forwards routes it did not originate.
    """
    origin = _origin_asn(path)
    if asn is not None and asn != observer_asn and origin is not None:
        return origin == asn
    return bool(set(path["nexthops"]) & addrs) or path.get("originator") in addrs


# ---------------------------------------------------------------- hijack


def _hijack_target(problem: Any, params: Any) -> str:
    return str(params.target_network or problem._default_target_network())


def _hijack_observer(problem: Any, params: Any) -> tuple[str | None, str | None]:
    """Remote router that should learn the hijack, and a host routed through it."""
    from nika.net_env.isp.inject_targets import stub_host_for_router

    observer = getattr(params, "symptom_host", None)
    if not observer:
        return None, None
    host = getattr(params, "observer_host", None) or stub_host_for_router(observer)
    return observer, host if host in problem.runtime.list_nodes() else None


def _hijack_reading(problem: Any, params: Any) -> dict[str, Any]:
    runtime = problem.runtime
    target = _hijack_target(problem, params)
    observer, stub = _hijack_observer(problem, params)
    reading: dict[str, Any] = {
        "target": target,
        "hijacker": params.host_name,
        "observer": observer,
        "observer_host": stub,
    }
    if observer is None:
        reading["error"] = "no_remote_observer_for_hijack"
        return reading
    hijacker_addrs = host_addresses(runtime, params.host_name)
    # The hijacker also holds the target address; it does not identify the hijacker.
    net = ipaddress.ip_network(target, strict=False)
    hijacker_addrs = {a for a in hijacker_addrs if ipaddress.ip_address(a) not in net}
    paths = prefix_paths(runtime, observer, target)
    reading["paths"] = paths
    if paths is None:
        reading["error"] = f"bgp_query_failed_on_{observer}"
        return reading
    # Behind an eBGP hub that rewrites the next hop, the hijacker's AS as the
    # path origin identifies it (``_from_router``).
    hijacker_asn = _local_asn(runtime, params.host_name)
    observer_asn = _local_asn(runtime, observer)
    reading["best_via_hijacker"] = any(
        path["best"] and _from_router(path, hijacker_addrs, hijacker_asn, observer_asn)
        for path in paths
    )
    probe_ip = getattr(params, "probe_dst_ip", None) or str(next(net.hosts()))
    reading["probe_ip"] = probe_ip
    if stub is not None:
        stats = ping_stats(runtime, stub, probe_ip, count=5, interval_sec=0.2)
        reading["host_reaches_target"] = stats.received > 0
        reading["host_ping_received"] = stats.received
        # Localization control: the same host still reaches the scenario's
        # default destination, which lies outside the hijacked prefix.
        path = get_probe_path(problem.scenario_name or "")
        control_ip = path.dst_ip if path is not None else None
        if control_ip and ipaddress.ip_address(control_ip) not in net:
            stats = ping_stats(runtime, stub, control_ip, count=5, interval_sec=0.2)
            reading["control_ip"] = control_ip
            reading["control_ok"] = stats.received > 0
    return reading


def hijack_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Before inject the observer has no route to the target via the hijacker."""
    reading = _hijack_reading(problem, params)
    ok = "error" not in reading and reading["best_via_hijacker"] is False
    return ok, reading


def hijack_attracts(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The remote observer's best path points at the hijacker and its host's traffic follows."""
    deadline = time.monotonic() + _ROUTE_WAIT_SEC
    while True:
        reading = _hijack_reading(problem, params)
        ok = (
            "error" not in reading
            and reading["best_via_hijacker"] is True
            and reading.get("host_reaches_target") is True
            and reading.get("control_ok") is not False
        )
        if ok or "error" in reading or time.monotonic() >= deadline:
            break
        time.sleep(_POLL_SEC)
    return ok, build_verify_result(
        fault_type=problem.root_cause_name, verified=ok, details=reading
    )


# ---------------------------------------------------------------- RPKI leak


def _rpki_reading(problem: Any) -> dict[str, Any]:
    runtime = problem.runtime
    bgp = problem._rpki_bgp_inventory()
    leaker_asn = int(bgp.get("leaker_asn") or 0)
    rov = str(bgp.get("rov_observer") or "")
    non_rov = str(bgp.get("non_rov_observer") or "")
    prefixes = [str(p) for p in bgp.get("leak_prefixes") or []]
    reading: dict[str, Any] = {
        "leaker_asn": leaker_asn,
        "rov_observer": rov,
        "non_rov_observer": non_rov,
        "prefixes": prefixes,
    }
    if not (leaker_asn and rov and non_rov and prefixes):
        reading["error"] = "rpki_inventory_incomplete"
        return reading
    for role, router in (("non_rov", non_rov), ("rov", rov)):
        learned: dict[str, bool] = {}
        for prefix in prefixes:
            paths = prefix_paths(runtime, router, prefix)
            if paths is None:
                reading["error"] = f"bgp_query_failed_on_{router}"
                return reading
            learned[prefix] = any(_origin_asn(p) == leaker_asn for p in paths)
        reading[f"{role}_learned_leaker_origin"] = learned
    cache = exec_or_empty(runtime, rov, "vtysh -c 'show rpki cache-connection'")
    reading["rov_cache_connected"] = "(connected)" in cache
    return reading


def rpki_leak_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Before inject neither observer holds a leak prefix with the leaker's origin."""
    reading = _rpki_reading(problem)
    ok = (
        "error" not in reading
        and reading["rov_cache_connected"]
        and not any(reading["non_rov_learned_leaker_origin"].values())
        and not any(reading["rov_learned_leaker_origin"].values())
    )
    return ok, reading


def rpki_leak_split(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The non-ROV observer accepts the leaked origin; the ROV observer rejects it."""
    deadline = time.monotonic() + _ROUTE_WAIT_SEC
    while True:
        reading = _rpki_reading(problem)
        ok = (
            "error" not in reading
            and reading["rov_cache_connected"]
            and all(reading["non_rov_learned_leaker_origin"].values())
            and not any(reading["rov_learned_leaker_origin"].values())
        )
        if ok or "error" in reading or time.monotonic() >= deadline:
            break
        time.sleep(_POLL_SEC)
    return ok, build_verify_result(
        fault_type=problem.root_cause_name, verified=ok, details=reading
    )


# ---------------------------------------------------------- missing advertisement


def _advertised_by(
    runtime: LabRuntime, observer: str, host: str, addrs: set[str], asn: int | None
) -> list[str] | None:
    table = _table(runtime, observer)
    if table is None:
        return None
    observer_asn = _local_asn(runtime, observer)
    return sorted(
        prefix
        for prefix, paths in table.items()
        if any(_from_router(path, addrs, asn, observer_asn) for path in paths)
    )


def advertisement_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Record the prefixes remote observers learn from the originator before inject."""
    runtime = problem.runtime
    host = params.host_name
    addrs = host_addresses(runtime, host)
    asn = _local_asn(runtime, host)
    wanted = getattr(params, "prefix", None)
    observers = (
        [params.symptom_host]
        if getattr(params, "symptom_host", None)
        else bgp_speakers_of(problem, host)
    )
    seen: dict[str, list[str]] = {}
    for observer in observers:
        learned = _advertised_by(runtime, observer, host, addrs, asn)
        if learned is None:
            continue
        if wanted:
            learned = [p for p in learned if p == str(ipaddress.ip_network(wanted))]
        if learned:
            seen[observer] = learned
    problem._audit_advertised = {
        "host": host,
        "addresses": sorted(addrs),
        "asn": asn,
        "seen": seen,
    }
    ok = bool(seen)
    return ok, {
        "host": host,
        "asn": asn,
        "observers": seen,
        **_data_path(problem, params),
    }


def _data_path(problem: Any, params: Any) -> dict[str, Any]:
    """Record (not judge) traffic from the observer toward the withdrawn prefix."""
    src = getattr(params, "symptom_host", None)
    dst = getattr(params, "probe_dst_ip", None)
    if not src or not dst:
        return {}
    stats = ping_stats(problem.runtime, src, dst, count=5, interval_sec=0.2)
    return {"data_path": f"{src}->{dst}", "data_path_received": stats.received}


def advertisement_withdrawn(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Every observer loses the originator's route for the baseline prefixes."""
    base = getattr(problem, "_audit_advertised", None)
    if (
        not isinstance(base, dict)
        or base.get("host") != params.host_name
        or not base.get("seen")
    ):
        return False, {"error": "no_healthy_advertisement_baseline"}
    runtime = problem.runtime
    addrs = set(base["addresses"])
    deadline = time.monotonic() + _ROUTE_WAIT_SEC
    while True:
        remaining: dict[str, list[str]] = {}
        errors: list[str] = []
        for observer, prefixes in base["seen"].items():
            learned = _advertised_by(
                runtime, observer, params.host_name, addrs, base["asn"]
            )
            if learned is None:
                errors.append(f"bgp_query_failed_on_{observer}")
                continue
            still = sorted(set(prefixes) & set(learned))
            if still:
                remaining[observer] = still
        ok = not errors and not remaining
        if ok or time.monotonic() >= deadline:
            break
        time.sleep(_POLL_SEC)
    details = {
        "host": params.host_name,
        "learned_before": base["seen"],
        "still_learned": remaining,
        "errors": errors,
        **_data_path(problem, params),
    }
    return ok, build_verify_result(
        fault_type=problem.root_cause_name, verified=ok, details=details
    )
