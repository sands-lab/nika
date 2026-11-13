"""Routing faults as the routers around the faulted one see them (test-path only).

A fault on one router shows up on its neighbors: their OSPF or IS-IS
adjacency with it, or their BGP session to it, goes down. These probes read
the neighbors' protocol state; circuit-health checks also read the target.
Each ``*_baseline`` records the healthy adjacencies before inject;
the matching symptom polls the same neighbors until the protocol timers have
run out and compares.
"""

from __future__ import annotations

import ipaddress
import json
import re
import time
from collections.abc import Callable, Iterable
from typing import Any

from nika.net_env.verify import exec_or_empty, ping_stats
from nika.problems.base import build_verify_result
from nika.problems.support.oracle import srl_cli
from nika.problems.support.router_ops import router_fault_ops, profile_routes_with_frr
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


def _ospf_neighbors(
    runtime: LabRuntime, router: str, *, profile: str | None = None
) -> list[dict] | None:
    if profile == "nokia_srlinux":
        return runtime.lab_api.srl_ospf_neighbors(router)
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
                        "iface": str(entry.get("ifaceName") or "").split(":")[0],
                    }
                )
    return rows


def _native_profile(problem: Any, node: str) -> str | None:
    from nika.problems.support.router_ops import native_router_profile

    net_env = getattr(problem, "net_env", None)
    if net_env is None:
        return None
    return native_router_profile(net_env, node)


def _isis_identity_ids(problem: Any, host: str) -> set[str]:
    """Names and system IDs remote IS-IS neighbors may use for ``host``."""
    ids = {host}
    plan = getattr(getattr(problem, "net_env", None), "plan", None)
    node = next(
        (item for item in getattr(plan, "nodes", ()) if item.device_name == host),
        None,
    )
    if node is None:
        return ids
    from nika.net_env.isp.igp.frr import isis_net_from_router_id

    parts = isis_net_from_router_id(node.router_id).split(".")
    ids.add(".".join(parts[2:5]))
    return ids


def _isis_neighbors(
    runtime: LabRuntime, router: str, *, profile: str | None = None
) -> list[dict] | None:
    if profile == "nokia_srlinux":
        return runtime.lab_api.srl_isis_neighbors(router)
    if profile == "iosxr":
        from nika.net_env.isp.iosxr import parse_iosxr_isis_adjacencies
        from nika.service.lab.iosxr_api import CLI_COMMAND

        out = exec_or_empty(
            runtime,
            router,
            CLI_COMMAND.format(command="show isis adjacency"),
            timeout=30,
        )
        try:
            return [
                {"system": system, "intf": intf, "state": "Up"}
                for system, intf in parse_iosxr_isis_adjacencies(out)
            ]
        except ValueError:
            return None
    if profile == "routeros":
        from nika.net_env.utils.routeros.parse import routeros_terse_rows
        from nika.service.lab.routeros_api import CLI_COMMAND

        out = exec_or_empty(
            runtime,
            router,
            CLI_COMMAND.format(command="/routing/isis/neighbor/print terse"),
            timeout=30,
        )
        try:
            rows = routeros_terse_rows(out)
        except ValueError:
            return None
        return [
            {
                "system": row.get("srcid", ""),
                "intf": row.get("interface", "").split("%", 1)[0],
                "state": row.get("state", ""),
            }
            for row in rows
        ]
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
        ops = router_fault_ops(problem, router)
        rows = (
            [
                {
                    "router_id": n.router_id,
                    "address": n.address,
                    "state": n.state,
                    "iface": n.interface,
                }
                for n in ops.ospf_neighbors()
            ]
            if ops is not None and not profile_routes_with_frr(ops.profile)
            else _ospf_neighbors(
                runtime,
                router,
                profile="nokia_srlinux"
                if srl_cli(problem.net_env, router)
                else _native_profile(problem, router),
            )
        )
        if rows is None:
            errors.append(f"ospf query failed on {router}")
            continue
        for row in rows:
            if row["address"] in addrs:
                states[f"ospf|{router}|{row['address']}"] = row["state"]
    host_ids = set(plan.get("host_ids") or (host,))
    for router in plan.get("isis", []):
        rows = _isis_neighbors(
            runtime,
            router,
            profile="nokia_srlinux"
            if srl_cli(problem.net_env, router)
            else _native_profile(problem, router),
        )
        if rows is None:
            errors.append(f"isis query failed on {router}")
            continue
        for row in rows:
            if row["system"] in host_ids:
                states[f"isis|{router}|{row['intf']}"] = row["state"]
    for router in plan.get("bgp", []):
        ops = (
            router_fault_ops(problem, router) if not router.startswith("pod:") else None
        )
        if ops is not None and not profile_routes_with_frr(ops.profile):
            established = ops.bgp_established_peers() & addrs
            for peer in established:
                states[f"bgp|{router}|{peer}"] = "Established"
            for key in plan.get("up", []):
                protocol, observer, peer = key.split("|")
                if (
                    protocol == "bgp"
                    and observer == router
                    and peer not in established
                    and ops.supports("bgp_last_notification")
                ):
                    notes[key] = {"notification": ops.bgp_last_notification(peer)}
            continue
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
    ops = router_fault_ops(problem, host)
    addresses = (
        ops.ipv4_addresses()
        if ops is not None
        else host_addresses(problem.runtime, host)
    )
    plan: dict[str, Any] = {
        "host_addresses": sorted(addresses),
        "facing": {router: sorted(intfs) for router, intfs in facing.items()},
        "host_ids": sorted(_isis_identity_ids(problem, host)),
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
            "host_ids": plan["host_ids"],
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


def bgp_some_lost(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Remote BGP peers of the router lose their sessions."""
    return watch_adjacencies(
        problem, params.host_name, _some_lost, expect="peers_drop_bgp"
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


def _ospf_routes(problem: Any, router: str) -> set[str]:
    """Active IPv4 prefixes the router learned through OSPF."""
    ops = router_fault_ops(problem, router)
    if ops is not None:
        return ops.ospf_routes()
    if problem.runtime.backend == "containerlab":
        return problem.runtime.lab_api.srl_routes(router, "ospf")
    out = _route_query(
        problem.runtime, router, "ip -4 route show proto ospf", timeout=15
    )
    if out is None:
        raise ValueError(f"Unreadable OSPF routes on {router!r}")
    return {line.split()[0] for line in out.splitlines() if line.strip()}


def _ospf_router_id(problem: Any, router: str) -> str | None:
    ops = router_fault_ops(problem, router)
    if ops is not None:
        return ops.ospf_router_id()
    if problem.runtime.backend == "containerlab":
        return problem.runtime.lab_api.srl_protocol_instance(router, "ospf")[
            "router-id"
        ]
    raw = exec_or_empty(problem.runtime, router, "vtysh -c 'show ip ospf json'")
    return (_load_json(raw) or {}).get("routerId")


# An LSA flood war between the two routers can run for minutes before one stops.
_DUPLICATE_ID_WAIT_SEC = 240.0


def _connected_routes(problem: Any, router: str) -> set[str]:
    ops = router_fault_ops(problem, router)
    if ops is not None:
        return ops.connected_routes()
    if problem.runtime.backend == "containerlab":
        return problem.runtime.lab_api.srl_routes(router, "local")
    out = _route_query(
        problem.runtime, router, "ip -4 route show proto kernel scope link", timeout=15
    )
    if out is None:
        raise ValueError(f"Unreadable connected routes on {router!r}")
    return {line.split()[0] for line in out.splitlines() if line.strip()}


def ospf_stub_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Each router's own subnets, the routers that reach them, and its router ID.

    A subnet counts when exactly one router is attached to it, so transit
    links that the far end also advertises are left out.
    """
    routers = lab_routers(problem)
    connected = {router: _connected_routes(problem, router) for router in routers}
    routed = {router: _ospf_routes(problem, router) for router in routers}
    stubs: dict[str, dict[str, list[str]]] = {}
    for owner, prefixes in connected.items():
        others = [router for router in routers if router != owner]
        own = prefixes - set().union(*(connected[r] for r in others))
        seen = {
            prefix: [r for r in others if prefix in routed[r]] for prefix in sorted(own)
        }
        if seen := {prefix: obs for prefix, obs in seen.items() if obs}:
            stubs[owner] = seen
    router_ids = {owner: _ospf_router_id(problem, owner) for owner in stubs}
    problem._audit_ospf_stubs = {"stubs": stubs, "router_ids": router_ids}
    ok = params.host_name in stubs
    return ok, {"host": params.host_name, "stub_routes": stubs}


def ospf_stubs_withdrawn(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The subnets of the router, or of the router whose ID it took, leave OSPF.

    Two routers with one router ID keep only one router-LSA between them, so
    every other router drops the subnets of one of the two.
    """
    audit = getattr(problem, "_audit_ospf_stubs", None) or {}
    stubs = audit.get("stubs") or {}
    host = params.host_name
    if host not in stubs:
        return False, {"error": "no_healthy_stub_routes", "host": host}
    router_id = _ospf_router_id(problem, host)
    twin = next(
        (
            owner
            for owner, owner_id in (audit.get("router_ids") or {}).items()
            if owner != host and owner_id == router_id
        ),
        None,
    )
    owners = [owner for owner in (host, twin) if owner]
    observers = sorted(
        {r for owner in owners for obs in stubs[owner].values() for r in obs}
    )

    def withdrawn_owner() -> str | None:
        routed = {router: _ospf_routes(problem, router) for router in observers}
        return next(
            (
                owner
                for owner in owners
                if not any(
                    prefix in routed[r]
                    for prefix, obs in stubs[owner].items()
                    for r in obs
                )
            ),
            None,
        )

    confirm_sec = 10.0
    started = time.monotonic()
    held_since: float | None = None
    while True:
        withdrawn = withdrawn_owner()
        elapsed = time.monotonic() - started
        if withdrawn:
            held_since = elapsed if held_since is None else held_since
            if elapsed - held_since >= confirm_sec:
                break
        else:
            held_since = None
        if elapsed >= _DUPLICATE_ID_WAIT_SEC + confirm_sec:
            withdrawn = None
            break
        time.sleep(_POLL_SEC)
    verified = withdrawn is not None
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "host": host,
            "router_id": router_id,
            "same_id_router": twin,
            "withdrawn_owner": withdrawn,
            "stub_routes": {owner: stubs[owner] for owner in owners},
        },
    )


_OSPF_LINK_WAIT_SEC = 90.0


def _ospf_intf_neighbor_full(problem: Any, host: str, intf: str) -> bool | None:
    """Whether ``host:intf`` holds a Full OSPF neighbor (native or FRR)."""
    from nika.problems.support.router_ops import router_fault_ops

    ops = router_fault_ops(problem, host)
    if ops is not None:
        if not ops.supports("ospf_interface_neighbor_full"):
            return None
        return ops.ospf_interface_neighbor_full(intf)
    rows = _ospf_neighbors(
        problem.runtime,
        host,
        profile="nokia_srlinux"
        if srl_cli(problem.net_env, host)
        else _native_profile(problem, host),
    )
    if rows is None:
        return None
    return any(
        _is_up("ospf", row["state"]) and row.get("iface") == intf for row in rows
    )


def _ospf_cut_edge_ends(problem: Any, host: str, intf: str) -> dict[str, Any]:
    """A host behind the stub router of ``host:intf`` and a host that is not."""
    from nika.net_env.net_env_pool import get_probe_path
    from nika.problems.routing_control_plane.ospf import (
        ospf_stub_router,
    )
    from nika.runtime.spec import NodeRole

    stub = ospf_stub_router(problem.net_env, host, intf)
    if stub is None:
        return {"error": "not_a_cut_edge", "host": host, "intf": intf}
    from nika.problems.support.benchmark_targets import hosts_behind_router

    behind = hosts_behind_router(problem.net_env, stub)
    dst_host = None
    dst = None
    for name in behind:
        addrs = sorted(host_addresses(problem.runtime, name))
        if addrs:
            dst_host = name
            dst = addrs[0]
            break
    identities = getattr(problem.net_env, "machine_identities", None) or {}
    topo_size = getattr(problem.net_env, "topo_size", None) or "s"
    path = get_probe_path(problem.scenario_name, topo_size=topo_size)
    behind_set = set(behind)
    candidates = [getattr(path, "src_host", None)]
    candidates.extend(
        sorted(
            node
            for node, identity in identities.items()
            if getattr(identity, "role", None) in {NodeRole.HOST, NodeRole.SERVICE}
        )
    )
    src = next((node for node in candidates if node and node not in behind_set), None)
    if not src or not dst:
        return {
            "error": "no_probe_ends",
            "host": host,
            "intf": intf,
            "stub": stub,
            "behind": behind,
        }
    return {
        "src": src,
        "dst": dst,
        "dst_host": dst_host,
        "stub": stub,
        "behind": behind,
    }


def ospf_full_unrouted_baseline(
    problem: Any, params: Any
) -> tuple[bool, dict[str, Any]]:
    """Full adjacency on the interface and a host behind the stub is reachable."""
    host, intf = params.host_name, params.intf_name
    ends = _ospf_cut_edge_ends(problem, host, intf)
    if "error" in ends:
        return False, ends
    deadline = time.monotonic() + _OSPF_LINK_WAIT_SEC
    loss = 100.0
    while True:
        full = _ospf_intf_neighbor_full(problem, host, intf)
        loss = ping_stats(
            problem.runtime, ends["src"], ends["dst"], count=5, interval_sec=0.2
        ).loss_percent
        if (full is True and loss == 0.0) or time.monotonic() >= deadline:
            break
        time.sleep(_POLL_SEC)
    problem._audit_ospf_cut_edge = {"host": host, "intf": intf, "full": full, **ends}
    ok = full is True and loss == 0.0
    return ok, {**ends, "host": host, "intf": intf, "full": full, "loss": loss}


def ospf_full_unrouted(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The neighbor stays Full while the stub host is no longer reachable."""
    base = getattr(problem, "_audit_ospf_cut_edge", None)
    host, intf = params.host_name, params.intf_name
    if not isinstance(base, dict) or base.get("host") != host:
        return False, {"error": "no_healthy_cut_edge_baseline", "host": host}
    deadline = time.monotonic() + _OSPF_LINK_WAIT_SEC
    full = None
    loss = 0.0
    while True:
        full = _ospf_intf_neighbor_full(problem, host, intf)
        loss = ping_stats(
            problem.runtime, base["src"], base["dst"], count=5, interval_sec=0.2
        ).loss_percent
        verified = full is True and loss == 100.0
        if verified or time.monotonic() >= deadline:
            break
        time.sleep(_POLL_SEC)
    verified = full is True and loss == 100.0
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "host": host,
            "intf": intf,
            "src": base["src"],
            "dst": base["dst"],
            "dst_host": base.get("dst_host"),
            "stub": base.get("stub"),
            "full": full,
            "loss": loss,
        },
    )


def ospf_cut_edge_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Record Full neighbors, learned routes, and reachability across the cut edge."""
    ends = _ospf_cut_edge_ends(problem, params.host_name, params.intf_name)
    if "error" in ends:
        return False, ends
    health = problem.link_health(params)
    prefixes = {
        node: [
            prefix
            for prefix in row["routes"]
            if ipaddress.ip_address(ends["dst"]) in ipaddress.ip_network(prefix)
        ]
        for node, row in health.items()
        if node != ends["stub"]
    }
    loss = ping_stats(
        problem.runtime, ends["src"], ends["dst"], count=5, interval_sec=0.2
    ).loss_percent
    ok = (
        all(row["full"] and row["interface_up"] for row in health.values())
        and any(prefixes.values())
        and loss == 0.0
    )
    base = {
        **ends,
        "host": params.host_name,
        "intf": params.intf_name,
        "link": health,
        "destination_routes": prefixes,
        "loss": loss,
    }
    if ok:
        problem._audit_ospf_cut_link = base
    return ok, base


def ospf_timer_mismatch(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Both adjacencies expire, the destination route withdraws, and host pings fail."""
    if not getattr(problem, "_audit_ospf_cut_link", None):
        return False, {"error": "no_healthy_cut_edge_baseline"}
    return _ospf_cut_edge_lost(
        problem,
        params,
        max(problem._original_timers[1], problem.injected_timers[1]) + 60,
    )


def ospf_stub_area_mismatch(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Stub disagreement removes the adjacency, learned routes, and host reachability."""
    return _ospf_cut_edge_lost(problem, params, 120)


def _ospf_cut_edge_lost(
    problem: Any, params: Any, timeout: float
) -> tuple[bool, dict[str, Any]]:
    base = getattr(problem, "_audit_ospf_cut_link", None)
    if not base or (base["host"], base["intf"]) != (params.host_name, params.intf_name):
        return False, {"error": "no_healthy_cut_edge_baseline"}
    deadline = time.monotonic() + timeout
    while True:
        health = problem.link_health(params)
        loss = ping_stats(
            problem.runtime, base["src"], base["dst"], count=5, interval_sec=0.2
        ).loss_percent
        withdrawn = all(
            not (set(prefixes) & set(health[node]["routes"]))
            for node, prefixes in base["destination_routes"].items()
        )
        ok = (
            all(not row["full"] and row["interface_up"] for row in health.values())
            and withdrawn
            and loss == 100.0
        )
        if ok or time.monotonic() >= deadline:
            break
        time.sleep(_POLL_SEC)
    return ok, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=ok,
        details={
            "src": base["src"],
            "dst": base["dst"],
            "link": health,
            "loss": loss,
            "destination_routes_withdrawn": withdrawn,
        },
    )


def _prefix(token: str) -> str | None:
    try:
        return str(ipaddress.ip_network(token, strict=False))
    except ValueError:
        return None


_QUERY_ERROR = re.compile(
    r"^\[TIMEOUT\]|syntax error|bad command|invalid input|unknown command|"
    r"command not found|cannot find device|no such device|operation not permitted",
    re.I | re.M,
)


def _route_query(runtime: LabRuntime, router: str, command: str, timeout: float):
    """Command output, or ``None`` when the command failed, timed out, or was rejected.

    Unlike ``exec_or_empty``, an empty answer stays an empty route table.
    """
    try:
        out = runtime.exec(
            router,
            f"( {command} ) || printf '\n[ROUTE_QUERY_FAILED]\n'",
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 - a failed read is not an empty table
        return None
    return None if "[ROUTE_QUERY_FAILED]" in out or _QUERY_ERROR.search(out) else out


def _isis_routes(problem: Any, router: str) -> set[str] | None:
    """Prefixes ``router`` installs from IS-IS; ``None`` when unreadable."""
    runtime = problem.runtime
    profile = _native_profile(problem, router)
    if getattr(runtime, "backend", None) == "containerlab" and profile is None:
        return runtime.lab_api.srl_routes(router, "isis")
    if profile == "iosxr":
        from nika.service.lab.iosxr_api import CLI_COMMAND

        out = _route_query(
            runtime, router, CLI_COMMAND.format(command="show route ipv4 isis"), 30
        )
        # XR always prints a header, so empty output is a failed read.
        if not out or not out.strip():
            return None
        found = {
            _prefix(token)
            for token in re.findall(
                r"^\s*i(?:\s+LE?[12])?\s+(\d+\.\d+\.\d+\.\d+/\d+)\s",
                out,
                re.M,
            )
        }
        return {prefix for prefix in found if prefix}
    if profile == "routeros":
        from nika.net_env.utils.routeros.parse import routeros_terse_rows
        from nika.service.lab.routeros_api import CLI_COMMAND

        out = _route_query(
            runtime,
            router,
            CLI_COMMAND.format(
                command="/routing/route/print terse where isis and active and afi=ip"
            ),
            30,
        )
        if out is None:
            return None
        try:
            rows = routeros_terse_rows(out)
        except ValueError:
            return None
        found = {_prefix(row["dst-address"]) for row in rows if "dst-address" in row}
        return {prefix for prefix in found if prefix}
    out = _route_query(runtime, router, "ip -4 route show proto isis", 15)
    if out is None:
        return None
    found = {_prefix(line.split()[0]) for line in out.splitlines() if line.strip()}
    return {prefix for prefix in found if prefix}


def _planned_stubs(problem: Any, host: str) -> set[str]:
    """Unique prefixes this router originates in the ISP plan, if present."""
    plan = getattr(getattr(problem, "net_env", None), "plan", None)
    node = next(
        (item for item in getattr(plan, "nodes", ()) if item.device_name == host),
        None,
    )
    if node is None:
        return set()
    prefixes = {_prefix(f"{node.loopback}/32")}
    prefixes.update(_prefix(iface.subnet) for iface in node.interfaces if iface.passive)
    return {prefix for prefix in prefixes if prefix}


def isis_stub_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The router's unique prefixes in IS-IS, plus Up adjacencies with it.

    A prefix counts when the ISP plan assigns it only to this router (loopback
    and passive edge subnets), or when kernel connected routes show the same
    uniqueness. Observers are the other routers that currently install it.
    """
    adj_ok, adj_details = adjacency_baseline(problem, params.host_name, ("isis",))
    routers = lab_routers(problem)
    planned = {router: _planned_stubs(problem, router) for router in routers}
    if any(planned.values()):
        own = planned
    else:
        connected = {
            router: {
                prefix
                for line in exec_or_empty(
                    problem.runtime,
                    router,
                    "ip -4 route show proto kernel scope link",
                    timeout=15,
                ).splitlines()
                if line.strip() and (prefix := _prefix(line.split()[0]))
            }
            for router in routers
        }
        own = {
            owner: prefixes
            - set().union(*(connected[r] for r in routers if r != owner))
            for owner, prefixes in connected.items()
        }
    problem._audit_isis_stubs = None
    routed = {router: _isis_routes(problem, router) for router in routers}
    unreadable = [router for router, routes in routed.items() if routes is None]
    if unreadable:
        return False, {"error": "isis_route_query_failed", "routers": unreadable}
    stubs: dict[str, dict[str, list[str]]] = {}
    for owner, prefixes in own.items():
        others = [router for router in routers if router != owner]
        seen = {
            prefix: [r for r in others if prefix in routed[r]]
            for prefix in sorted(prefixes)
        }
        if seen := {prefix: obs for prefix, obs in seen.items() if obs}:
            stubs[owner] = seen
    problem._audit_isis_stubs = {
        "stubs": stubs,
        "adjacencies_up": adj_details.get("adjacencies_up"),
    }
    # Native routers may not expose IPv4 addresses in the Linux namespace
    # (XRd), so adjacency_baseline's host_addresses gate is not required.
    ok = bool(adj_details.get("adjacencies_up")) and params.host_name in stubs
    return ok, {
        "host": params.host_name,
        "stub_routes": stubs,
        "adjacencies_up": adj_details.get("adjacencies_up"),
        "adjacency_baseline_ok": adj_ok,
    }


def isis_stubs_withdrawn(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The router's unique prefixes leave IS-IS while its adjacencies stay Up.

    Neighbors still exchange hellos; they stop installing each other's IP
    reachability because the metric-style TLVs no longer match.
    """
    audit = getattr(problem, "_audit_isis_stubs", None) or {}
    stubs = audit.get("stubs") or {}
    host = params.host_name
    if host not in stubs:
        return False, {"error": "no_healthy_stub_routes", "host": host}
    observers = sorted({r for obs in stubs[host].values() for r in obs})
    confirm_sec = 10.0
    started = time.monotonic()
    held_since: float | None = None
    remaining: dict[str, list[str]] = {}
    lost: list[str] = []
    while True:
        routed: dict[str, set[str]] = {}
        query_ok = True
        for router in observers:
            prefixes = _isis_routes(problem, router)
            if prefixes is None:
                query_ok = False
                routed[router] = set()
            else:
                routed[router] = prefixes
        remaining = {
            prefix: [r for r in obs if prefix in routed[r]]
            for prefix, obs in stubs[host].items()
        }
        prefixes_gone = not any(remaining.values())
        plan = getattr(problem, "_audit_routing_plan", None) or {}
        states, _notes, errors = _read_view(problem, host, plan)
        lost = [
            key
            for key in plan.get("up", [])
            if not _is_up(key.split("|")[0], states.get(key))
        ]
        elapsed = time.monotonic() - started
        ok = prefixes_gone and not lost and not errors and query_ok
        if ok:
            held_since = elapsed if held_since is None else held_since
            if elapsed - held_since >= confirm_sec:
                break
        else:
            held_since = None
        if elapsed >= _IGP_WAIT_SEC + confirm_sec:
            break
        time.sleep(_POLL_SEC)
    verified = held_since is not None and elapsed - held_since >= confirm_sec
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "host": host,
            "stub_routes": stubs[host],
            "remaining": remaining,
            "adjacencies_up_before": plan.get("up", []),
            "lost": lost,
            "errors": errors,
        },
    )


def isis_adj_and_stubs_lost(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Neighbors drop the IS-IS adjacency and stop installing the router's prefixes.

    Checks the routers that held an Up adjacency with the host. Each unique
    prefix must leave at least one of those neighbors; a leftover LSP can
    linger on some routers until remaining lifetime expires.
    """
    audit = getattr(problem, "_audit_isis_stubs", None) or {}
    stubs = audit.get("stubs") or {}
    host = params.host_name
    if host not in stubs:
        return False, {"error": "no_healthy_stub_routes", "host": host}
    plan = getattr(problem, "_audit_routing_plan", None) or {}
    neighbors = sorted({key.split("|")[1] for key in plan.get("up", [])})
    if not neighbors:
        return False, {"error": "no_healthy_adjacency_baseline", "host": host}
    neighbor_stubs = {
        prefix: [router for router in obs if router in neighbors]
        for prefix, obs in stubs[host].items()
    }
    neighbor_stubs = {prefix: obs for prefix, obs in neighbor_stubs.items() if obs}
    if not neighbor_stubs:
        return False, {"error": "no_neighbor_stub_routes", "host": host}
    confirm_sec = 10.0
    started = time.monotonic()
    held_since: float | None = None
    remaining: dict[str, list[str]] = {}
    lost: list[str] = []
    kept: list[str] = []
    errors: list[str] = []
    while True:
        routed: dict[str, set[str]] = {}
        query_ok = True
        for router in neighbors:
            prefixes = _isis_routes(problem, router)
            if prefixes is None:
                query_ok = False
                routed[router] = set()
            else:
                routed[router] = prefixes
        remaining = {
            prefix: [router for router in obs if prefix in routed[router]]
            for prefix, obs in neighbor_stubs.items()
        }
        prefixes_withdrawn = all(
            len(remaining[prefix]) < len(obs) for prefix, obs in neighbor_stubs.items()
        )
        states, _notes, errors = _read_view(problem, host, plan)
        lost = [
            key
            for key in plan.get("up", [])
            if not _is_up(key.split("|")[0], states.get(key))
        ]
        kept = [key for key in plan.get("up", []) if key not in lost]
        elapsed = time.monotonic() - started
        ok = prefixes_withdrawn and bool(lost) and not kept and not errors and query_ok
        if ok:
            held_since = elapsed if held_since is None else held_since
            if elapsed - held_since >= confirm_sec:
                break
        else:
            held_since = None
        if elapsed >= _IGP_WAIT_SEC + confirm_sec:
            break
        time.sleep(_POLL_SEC)
    verified = held_since is not None and elapsed - held_since >= confirm_sec
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "host": host,
            "stub_routes": neighbor_stubs,
            "remaining": remaining,
            "adjacencies_up_before": plan.get("up", []),
            "lost": lost,
            "kept": kept,
            "errors": errors,
        },
    )


def _isis_live_interfaces(problem: Any, host: str) -> dict[str, str]:
    """IS-IS circuit states from the running protocol, independent of adjacencies."""
    if (
        problem.runtime.backend == "containerlab"
        and _native_profile(problem, host) is None
    ):
        from nika.service.containerlab.srl_api import srl_linux_intf

        instance = problem.runtime.lab_api.srl_protocol_instance(host, "isis")
        return {
            srl_linux_intf(i["interface-name"]): i["oper-state"].capitalize()
            for i in instance["interface"]
        }
    if _native_profile(problem, host) == "iosxr":
        from nika.service.lab.iosxr_api import CLI_COMMAND

        raw = problem.runtime.exec(
            host, CLI_COMMAND.format(command="show isis interface brief"), timeout=90
        )
        return {
            re.sub(r"^Gi(?=\d)", "GigabitEthernet", name): state
            for name, state in re.findall(
                r"^\s*(\S+)\s+(?:Yes|No)\s+\S+\s+\S+\s+\S+\s+\S+\s+(Up|Down)\b",
                raw,
                re.M,
            )
        }
    raw = problem.runtime.exec(
        host, "vtysh -c 'show isis interface detail'", timeout=30
    )
    states = dict(re.findall(r"Interface: (\S+), State: (\S+), Active", raw))
    if _native_profile(problem, host) == "sonic":
        from nika.net_env.utils.sonic.common import container_iface

        return {container_iface(port): state for port, state in states.items()}
    return states


def isis_authentication_baseline(
    problem: Any, params: Any
) -> tuple[bool, dict[str, Any]]:
    ok, details = isis_stub_baseline(problem, params)
    modes = problem._authentication(params.host_name)
    states = _isis_live_interfaces(problem, params.host_name)
    circuits = sorted(modes)
    problem._audit_isis_hello_circuits = circuits
    details.update({"hello_authentication": modes, "circuit_states": states})
    return (
        ok
        and bool(circuits)
        and all(modes[c] == "none" and states.get(c) == "Up" for c in circuits),
        details,
    )


def isis_authentication_lost(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    circuits = getattr(problem, "_audit_isis_hello_circuits", [])
    if not circuits:
        return False, {"error": "no_healthy_circuit_baseline", "host": params.host_name}
    ok, result = isis_adj_and_stubs_lost(problem, params)
    states = _isis_live_interfaces(problem, params.host_name)
    running = bool(circuits) and all(states.get(c) == "Up" for c in circuits)
    result.setdefault("details", {})["circuit_states"] = states
    result["details"]["isis_circuits_running"] = running
    result["verified"] = ok and running
    return ok and running, result
