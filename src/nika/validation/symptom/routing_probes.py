"""Routing faults as the routers around the faulted one see them (test-path only).

A fault on one router shows up on its neighbors: their OSPF or IS-IS
adjacency with it, or their BGP session to it, goes down. These probes read
only the neighbors, never the faulted router's own protocol state. Each
``*_baseline`` records the healthy adjacencies on the problem before inject;
the matching symptom polls the same neighbors until the protocol timers have
run out and compares.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable
from typing import Any

from nika.net_env.verify import exec_or_empty, ping_stats
from nika.problems.base import build_verify_result
from nika.problems.rca.inventory import iter_link_termination_points, parse_endpoint
from nika.runtime.base import LabRuntime
from nika.validation.symptom.addressing_probes import _HOST_PREFIXES

# OSPF dead (40 s) and IS-IS hold (30 s) intervals, plus SPF and polling slack.
_IGP_WAIT_SEC = 90.0
# Added to the negotiated BGP hold time: a dropped session can stay Established
# on the far side until its hold timer expires.
_BGP_SLACK_SEC = 30.0
_POLL_SEC = 3.0
_UP = {"ospf": "full", "isis": "up", "bgp": "established"}


def _load_json(raw: str) -> Any:
    text = raw.strip()
    if not text.startswith("{"):
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def host_addresses(runtime: LabRuntime, node: str) -> set[str]:
    """IPv4 addresses on ``node`` (kernel, or the SR Linux default namespace)."""
    out = exec_or_empty(
        runtime,
        node,
        "ip -4 -o addr show 2>/dev/null; "
        "ip netns exec srbase-default ip -4 -o addr show 2>/dev/null; true",
        timeout=15,
    )
    return {
        addr
        for addr in re.findall(r"inet (\d+\.\d+\.\d+\.\d+)/", out)
        if not addr.startswith("127.")
    }


def lab_routers(problem: Any) -> list[str]:
    """Router names from the scenario's machine roles, else non-host lab nodes."""
    net_env = problem.net_env
    routers = getattr(net_env, "routers", None)
    if not routers:
        try:
            net_env.load_machines()
            routers = net_env.routers
        except Exception:  # noqa: BLE001 - labs without a Kathara definition
            routers = None
    if not routers:
        routers = [
            node
            for node in problem.runtime.list_nodes()
            if not node.startswith(_HOST_PREFIXES)
        ]
    return sorted(routers)


def facing_interfaces(problem: Any, host: str) -> dict[str, set[str]]:
    """Routers linked to ``host``, each with its interfaces on those links."""
    net_env = problem.net_env
    routers = set(lab_routers(problem))
    facing: dict[str, set[str]] = {}
    for _key, tps in iter_link_termination_points(net_env):
        ends = [parse_endpoint(tp) for tp in tps]
        if host not in {node for node, _ in ends}:
            continue
        for node, intf in ends:
            if node and intf and node != host and node in routers:
                facing.setdefault(node, set()).add(intf)
    return facing


def bgp_speakers_of(problem: Any, host: str) -> list[str]:
    """Routers that hold a BGP session to ``host``.

    ISP labs list every session in their inventory. Other labs are scanned in
    full, and routers without a session to ``host`` drop out at baseline.
    """
    inventory = getattr(problem.net_env, "inventory", None)
    bgp = inventory.get("bgp") if isinstance(inventory, dict) else None
    sessions = bgp.get("sessions") if isinstance(bgp, dict) else None
    if sessions:
        return sorted(
            {
                str(s["local_device"])
                for s in sessions
                if s.get("remote_device") == host and s.get("local_device")
            }
        )
    return sorted(set(lab_routers(problem)) - {host}) + _cluster_bgp_speakers(problem)


def _cluster_bgp_speakers(problem: Any) -> list[str]:
    """In-cluster BGP speakers (MetalLB in FRR mode) as ``pod:<node>/<ns>/<pod>``."""
    from nika.problems.support.kubernetes.base import control_node_from_net_env

    k8s = getattr(problem.runtime, "lab_api", None)
    control = control_node_from_net_env(problem.net_env)
    if not control or not hasattr(k8s, "kubectl_json"):
        return []
    try:
        pods = k8s.kubectl_json(control, "get pods -A -l component=speaker")
    except Exception:  # noqa: BLE001 - a cluster without MetalLB has no speakers
        return []
    return sorted(
        f"pod:{control}/{item['metadata']['namespace']}/{item['metadata']['name']}"
        for item in pods.get("items", [])
    )


def _vtysh(runtime: LabRuntime, router: str, command: str) -> str:
    """Run vtysh on a lab node, or in the ``frr`` container of a ``pod:`` speaker."""
    if not router.startswith("pod:"):
        return exec_or_empty(
            runtime, router, f"vtysh -c '{command}' 2>/dev/null", timeout=30
        )
    control, namespace, pod = router[len("pod:") :].split("/", 2)
    try:
        result = runtime.lab_api.kubectl(
            control,
            f"exec -n {namespace} {pod} -c frr -- vtysh -c '{command}'",
            check=False,
        )
    except Exception:  # noqa: BLE001 - an unreachable speaker is an unreadable observer
        return ""
    return result.stdout


def bgp_neighbors(runtime: LabRuntime, router: str) -> dict[str, dict] | None:
    """Per-peer BGP state on ``router`` (FRR or SR Linux); ``None`` when unreadable."""
    data = _load_json(_vtysh(runtime, router, "show bgp vrf all neighbors json"))
    if isinstance(data, dict):
        peers: dict[str, dict] = {}
        for vrf in data.values():
            if not isinstance(vrf, dict):
                continue
            for key, peer in vrf.items():
                if isinstance(peer, dict) and "bgpState" in peer:
                    peers[key] = {
                        "state": str(peer["bgpState"]),
                        "hostname": peer.get("hostname"),
                        "hold_ms": peer.get("bgpTimerHoldTimeMsecs"),
                        "notification": peer.get("lastNotificationReason"),
                        "reset": peer.get("lastResetDueTo"),
                        "connect_retry": peer.get("connectRetryTimer"),
                    }
        return peers
    if router.startswith("pod:"):
        return None
    out = exec_or_empty(
        runtime,
        router,
        'sr_cli "show network-instance default protocols bgp neighbor" 2>/dev/null',
        timeout=30,
    )
    if not re.search(r"\d+ configured sessions", out, re.IGNORECASE):
        return None
    peers = {}
    for line in out.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) > 6 and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", cells[2]):
            peers[cells[2]] = {"state": cells[6], "notification": None}
    # SR Linux keeps the last NOTIFICATION per neighbor in its state tree.
    state = exec_or_empty(
        runtime,
        router,
        'sr_cli "info flat from state network-instance default protocols bgp '
        'neighbor *" 2>/dev/null | grep -i notification',
        timeout=30,
    )
    for line in state.splitlines():
        match = re.search(r"neighbor (\d+\.\d+\.\d+\.\d+) (.*notification.*)$", line)
        if match and match.group(1) in peers:
            text = " ".join(
                filter(None, [peers[match.group(1)]["notification"], match.group(2)])
            )
            peers[match.group(1)]["notification"] = text
    return peers


def _ospf_neighbors(runtime: LabRuntime, router: str) -> list[dict] | None:
    data = _load_json(
        exec_or_empty(
            runtime, router, "vtysh -c 'show ip ospf neighbor json' 2>/dev/null"
        )
    )
    if not isinstance(data, dict) or not isinstance(data.get("neighbors"), dict):
        return None
    rows: list[dict] = []
    for router_id, entries in data["neighbors"].items():
        for entry in entries if isinstance(entries, list) else [entries]:
            if isinstance(entry, dict):
                rows.append(
                    {
                        "router_id": router_id,
                        "address": entry.get("ifaceAddress"),
                        "state": str(entry.get("nbrState") or ""),
                    }
                )
    return rows


def _isis_neighbors(runtime: LabRuntime, router: str) -> list[dict] | None:
    out = exec_or_empty(runtime, router, "vtysh -c 'show isis neighbor' 2>/dev/null")
    if "System Id" not in out:
        return None
    rows: list[dict] = []
    for line in out.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[2].isdigit():
            rows.append({"system": fields[0], "intf": fields[1], "state": fields[3]})
    return rows


def _is_up(protocol: str, state: str | None) -> bool:
    return bool(state) and str(state).lower().startswith(_UP[protocol])


def _read_view(
    problem: Any, host: str, plan: dict[str, Any]
) -> tuple[dict[str, str], dict[str, dict], list[str]]:
    """Current state of every planned remote adjacency, keyed ``proto|router|peer``."""
    runtime = problem.runtime
    states: dict[str, str] = {}
    notes: dict[str, dict] = {}
    errors: list[str] = []
    addrs = set(plan["host_addresses"])
    facing = {router: set(intfs) for router, intfs in plan["facing"].items()}
    for router in plan.get("ospf", []):
        rows = _ospf_neighbors(runtime, router)
        if rows is None:
            errors.append(f"ospf query failed on {router}")
            continue
        for row in rows:
            if row["address"] in addrs:
                states[f"ospf|{router}|{row['address']}"] = row["state"]
    for router in plan.get("isis", []):
        rows = _isis_neighbors(runtime, router)
        if rows is None:
            errors.append(f"isis query failed on {router}")
            continue
        for row in rows:
            if row["system"] == host:
                states[f"isis|{router}|{row['intf']}"] = row["state"]
    for router in plan.get("bgp", []):
        peers = bgp_neighbors(runtime, router)
        if peers is None:
            errors.append(f"bgp query failed on {router}")
            continue
        for key, peer in peers.items():
            if (
                key in addrs
                or key in facing.get(router, set())
                or peer.get("hostname") == host
            ):
                states[f"bgp|{router}|{key}"] = peer["state"]
                notes[f"bgp|{router}|{key}"] = peer
    return states, notes, errors


def adjacency_baseline(
    problem: Any,
    host: str,
    protocols: Iterable[str],
    *,
    bgp_observers: list[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Record which neighbors hold an up adjacency with ``host`` before inject."""
    facing = facing_interfaces(problem, host)
    candidates = {
        "ospf": sorted(facing),
        "isis": sorted(facing),
        "bgp": bgp_observers or bgp_speakers_of(problem, host),
    }
    plan: dict[str, Any] = {
        "host_addresses": sorted(host_addresses(problem.runtime, host)),
        "facing": {router: sorted(intfs) for router, intfs in facing.items()},
    }
    wanted = set(protocols)
    states: dict[str, str] = {}
    notes: dict[str, dict] = {}
    for protocol in ("ospf", "isis", "bgp"):
        if protocol not in wanted:
            continue
        probe_plan = {
            "host_addresses": plan["host_addresses"],
            "facing": plan["facing"],
            protocol: candidates[protocol],
        }
        found, found_notes, _errors = _read_view(problem, host, probe_plan)
        # A router that does not run the protocol, or holds no adjacency with
        # the host, is not an observer.
        observers = sorted({key.split("|")[1] for key in found})
        plan[protocol] = observers
        states.update(found)
        notes.update(found_notes)
    up = sorted(
        key for key, state in states.items() if _is_up(key.split("|")[0], state)
    )
    hold = [
        int(note["hold_ms"]) / 1000.0
        for key, note in notes.items()
        if key in up and isinstance(note.get("hold_ms"), int)
    ]
    plan["bgp_hold_sec"] = max(hold) if hold else None
    retry = [
        int(note["connect_retry"])
        for key, note in notes.items()
        if key in up and isinstance(note.get("connect_retry"), int)
    ]
    plan["bgp_connect_retry_sec"] = max(retry) if retry else None
    plan["up"] = up
    problem._audit_routing_plan = {"host": host, **plan}
    ok = bool(up) and bool(plan["host_addresses"])
    return ok, {
        "host": host,
        "observers": {p: plan.get(p, []) for p in ("ospf", "isis", "bgp")},
        "adjacencies_up": up,
        "states": states,
    }


def watch_adjacencies(
    problem: Any,
    host: str,
    decide: Callable[[list[str], list[str], dict[str, dict]], bool],
    *,
    expect: str,
    confirm_sec: float = 0.0,
    reconnect: bool = False,
) -> tuple[bool, dict[str, Any]]:
    """Poll the baseline's observers until ``decide(lost, kept, notes)`` holds.

    Polling stops at the protocol's timer bound (OSPF dead / IS-IS hold, or the
    negotiated BGP hold time). ``reconnect`` adds the observers' BGP
    ConnectRetry timer, for outcomes that need a peer to open a new session.
    ``confirm_sec`` requires the outcome to hold for that long, so a transient
    outage is not read as the fault. A query that fails on any observer makes
    that read inconclusive.
    """
    plan = getattr(problem, "_audit_routing_plan", None)
    if not isinstance(plan, dict) or plan.get("host") != host:
        return False, {"error": "no_healthy_adjacency_baseline", "host": host}
    protocols = {key.split("|")[0] for key in plan["up"]}
    wait = _IGP_WAIT_SEC if protocols & {"ospf", "isis"} else 0.0
    if "bgp" in protocols:
        wait = max(wait, float(plan.get("bgp_hold_sec") or 180.0) + _BGP_SLACK_SEC)
        if reconnect:
            wait += float(plan.get("bgp_connect_retry_sec") or 120.0)
    wait += confirm_sec
    started = time.monotonic()
    held_since: float | None = None
    history: list[dict[str, Any]] = []
    while True:
        states, notes, errors = _read_view(problem, host, plan)
        lost = [
            key for key in plan["up"] if not _is_up(key.split("|")[0], states.get(key))
        ]
        kept = [key for key in plan["up"] if key not in lost]
        elapsed = time.monotonic() - started
        ok = not errors and decide(lost, kept, notes)
        step = {"lost": len(lost), "kept": len(kept), "ok": ok}
        if not history or {k: history[-1][k] for k in step} != step:
            history.append({"t": round(elapsed, 1), **step})
        if ok:
            held_since = elapsed if held_since is None else held_since
            if elapsed - held_since >= confirm_sec:
                break
        else:
            held_since = None
        if elapsed >= wait:
            break
        time.sleep(_POLL_SEC)
    verified = held_since is not None and elapsed - held_since >= confirm_sec
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "expect": expect,
            "host": host,
            "adjacencies_up_before": plan["up"],
            "lost": lost,
            "kept": kept,
            "states_after": {key: states.get(key, "absent") for key in plan["up"]},
            "peer_notes": {key: notes[key] for key in lost if key in notes},
            "errors": errors,
            "wait_bound_sec": wait,
            "history": history,
        },
    )


def _all_lost(lost: list[str], kept: list[str], _notes: dict) -> bool:
    return bool(lost) and not kept


def _some_lost(lost: list[str], kept: list[str], _notes: dict) -> bool:
    return bool(lost)


def _one_lost(lost: list[str], kept: list[str], _notes: dict) -> bool:
    # One adjacency goes away; the router keeps its others (it is not isolated).
    return len(lost) == 1 and (bool(kept) or len(lost) + len(kept) == 1)


def ospf_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    return adjacency_baseline(problem, params.host_name, ("ospf",))


def ospf_all_lost(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Every remote OSPF neighbor of the router drops its adjacency."""
    return watch_adjacencies(
        problem, params.host_name, _all_lost, expect="every_neighbor_drops_ospf"
    )


def ospf_some_lost(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Some remote OSPF neighbors of the router drop their adjacency."""
    return watch_adjacencies(
        problem, params.host_name, _some_lost, expect="neighbors_drop_ospf"
    )


def ospf_one_lost(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """One remote OSPF neighbor drops its adjacency; the router keeps the others."""
    return watch_adjacencies(
        problem, params.host_name, _one_lost, expect="one_neighbor_drops_ospf"
    )


def bgp_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    return adjacency_baseline(problem, params.host_name, ("bgp",))


def bgp_all_lost(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Every remote BGP peer of the router loses its session."""
    return watch_adjacencies(
        problem, params.host_name, _all_lost, expect="every_peer_drops_bgp"
    )


def _bad_peer_as(lost: list[str], kept: list[str], notes: dict) -> bool:
    # A peer that expects the router's old AS refuses its OPEN with
    # "Bad Peer AS" and the session stays down. Peers configured with
    # ``remote-as external`` accept any AS and may stay up.
    return any(
        "bad peer as"
        in re.sub(
            r"[-_]", " ", str(notes.get(key, {}).get("notification") or "")
        ).lower()
        for key in lost
    )


def bgp_bad_peer_as(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Remote peers refuse the router's OPEN with "Bad Peer AS"; sessions stay down."""
    # The injector restarts bgpd, so the loss must outlast a normal restart.
    return watch_adjacencies(
        problem,
        params.host_name,
        _bad_peer_as,
        expect="peers_refuse_the_router_with_bad_peer_as",
        confirm_sec=30.0,
        reconnect=True,
    )


def routing_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    return adjacency_baseline(problem, params.host_name, ("ospf", "isis", "bgp"))


def routing_all_lost(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Every remote OSPF, IS-IS, or BGP neighbor of the router loses its adjacency."""
    return watch_adjacencies(
        problem, params.host_name, _all_lost, expect="every_neighbor_drops_routing"
    )


def _customer_reach(problem: Any, params: Any) -> dict[str, Any] | None:
    """Pings from the customer site whose only route rides the session."""
    site = problem.customer_route(params.receiver_name, params.peer_name)
    if site is None:
        return None
    host, target = str(site["host"]), str(site["service_address"])
    stats = ping_stats(problem.runtime, host, target, count=5, interval_sec=0.2)
    return {"customer_host": host, "service": target, "received": stats.received}


def max_prefix_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The flooding peer's session is up and the customer site reaches its service."""
    ok, details = adjacency_baseline(
        problem, params.receiver_name, ("bgp",), bgp_observers=[params.peer_name]
    )
    reach = _customer_reach(problem, params)
    if reach is not None:
        details["customer_reach"] = reach
        ok = ok and reach["received"] > 0
    return ok, details


def _max_prefix_cease(lost: list[str], kept: list[str], notes: dict) -> bool:
    # Only an exceeded limit makes the receiver send this Cease subcode.
    return _all_lost(lost, kept, notes) and all(
        "maximum number of prefixes"
        in str(notes.get(key, {}).get("notification") or "").lower()
        for key in lost
    )


def max_prefix_cease(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The peer's session is down with the receiver's max-prefix Cease notification.

    The customer site whose route rode only that session loses its service.
    """
    ok, result = watch_adjacencies(
        problem,
        params.receiver_name,
        _max_prefix_cease,
        expect="peer_receives_cease_maximum_prefixes_and_session_stays_down",
    )
    reach = _customer_reach(problem, params)
    if reach is None:
        return ok, result
    # Withdrawals reach the far end of the receiver's AS within seconds.
    deadline = time.monotonic() + 30.0
    while reach["received"] and time.monotonic() < deadline:
        time.sleep(_POLL_SEC)
        reach = _customer_reach(problem, params) or reach
    ok = ok and reach["received"] == 0
    details = dict(result.get("details") or {})
    details["customer_reach"] = reach
    return ok, build_verify_result(
        fault_type=problem.root_cause_name, verified=ok, details=details
    )
