"""Symptom probes for P4Runtime forwarding-state faults on BMv2."""

from __future__ import annotations

import ipaddress
import json
import shlex
import time
from typing import Any

from nika.net_env.p4_dc_fabric.fabric_manager.apply import INTENT_PATH, P4INFO_PATH
from nika.net_env.verify import http_ok, ping_stats
from nika.problems.base import build_verify_result
from nika.net_env.net_env_pool import get_probe_path
from nika.problems.forwarding_encapsulation_policy.p4_runtime import (
    _BAD_PORT,
    partial_write_routes,
)
from nika.problems.forwarding_encapsulation_policy.p4runtime_helpers import (
    ecmp_target,
    load_intent,
    lpm_capacity,
    run_manager,
)


# One UDP datagram per fixed source port toward a host behind the faulted ECMP
# group. The CRC16 selector is linear, so the ports are spread over the range.
_SWEEP_SOURCES = [10007 + 211 * index for index in range(256)]
_SWEEP_PORT = 19411
_SWEEP_LOG = "/tmp/nika-audit-sweep.log"
_SWEEP_PID = "/tmp/nika-audit-sweep.pid"
_SWEEP_MIN_DELIVERED = 0.98
_SWEEP_LISTEN = r"""
import socket, sys
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", int(sys.argv[1])))
log = open(sys.argv[2], "a", buffering=1)
while True:
    data, _ = sock.recvfrom(64)
    log.write(data.decode("ascii") + "\n")
"""
_SWEEP_SEND = r"""
import socket, sys, time
dst, port = sys.argv[1], int(sys.argv[2])
sources = [int(item) for item in sys.argv[3].split(",")]
for source in sources:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", source))
    sock.sendto(str(source).encode(), (dst, port))
    sock.close()
    time.sleep(0.01)
print("sent", len(sources))
"""


def _sweep_ends(problem: Any, params: Any) -> dict[str, Any]:
    """Resolve the faulted group's member ports and a host pair that uses it."""
    intent = load_intent(problem.runtime)
    prefix, group_id, member_id, _peer = ecmp_target(intent, params.host_name)
    state = intent["switches"][params.host_name]
    ports = {int(m["member_id"]): int(m["port"]) for m in state["members"]}
    peers = {str(m["peer"]) for m in state["members"]}
    group = next(g for g in state["groups"] if int(g["group_id"]) == group_id)
    network = ipaddress.ip_network(prefix)
    endpoints = problem.net_env.model.endpoints
    target = next(e for e in endpoints if ipaddress.ip_address(e.ip) in network)
    # Each group member's next hop and the port on which it receives from us.
    next_hops: dict[int, list] = {}
    for member in state["members"]:
        far = intent["switches"].get(str(member["peer"]))
        if int(member["member_id"]) not in map(int, group["member_ids"]) or not far:
            continue
        port = next(
            (int(m["port"]) for m in far["members"] if m["peer"] == params.host_name),
            None,
        )
        if port is not None:
            next_hops[int(member["port"])] = [str(member["peer"]), port]
    return {
        "switch": params.host_name,
        "source": next(e.name for e in endpoints if e.name in peers),
        "target": target.name,
        "target_ip": target.ip,
        "prefix": prefix,
        "member_port": ports[member_id],
        "other_ports": sorted(
            ports[int(m)] for m in group["member_ids"] if int(m) != member_id
        ),
        "next_hops": next_hops,
    }


def _port_packets(problem: Any, switch: str, direction: str) -> dict[int, int]:
    observed = run_manager(problem.runtime, "counters", "--switch", switch, timeout=60)
    counters = (observed.get("counters") or {}).get(switch)
    if not observed.get("ok") or counters is None:
        raise RuntimeError(f"counter read failed on {switch}: {observed}")
    return {
        int(port): int(value["packets"])
        for port, value in (counters.get(direction) or {}).items()
    }


def _egress_packets(problem: Any, switch: str) -> dict[int, int]:
    return _port_packets(problem, switch, "egress")


def _next_hop_packets(problem: Any, ends: dict[str, Any]) -> dict[int, int]:
    """Packets each member's next hop received from the switch, by member port."""
    return {
        int(port): _port_packets(problem, hop, "ingress").get(int(hop_port), 0)
        for port, (hop, hop_port) in ends["next_hops"].items()
    }


def _sweep(
    problem: Any, ends: dict[str, Any], *, next_hops: bool = False
) -> dict[str, Any]:
    """Send every flow once; return delivered flows and per-port packet deltas.

    With ``next_hops`` the deltas are also read on each member's next hop.
    """
    runtime = problem.runtime
    started = runtime.exec(
        ends["target"],
        f"rm -f {_SWEEP_LOG}; nohup timeout 120 python3 -u -c "
        f"{shlex.quote(_SWEEP_LISTEN)} {_SWEEP_PORT} {_SWEEP_LOG} "
        f">/dev/null 2>&1 </dev/null & echo $! > {_SWEEP_PID}; "
        f"sleep 0.5; kill -0 $(cat {_SWEEP_PID}) && echo listening",
        timeout=20,
    )
    try:
        if started.strip() != "listening":
            return {"error": f"sweep listener did not start: {started[:300]}"}
        before = _egress_packets(problem, ends["switch"])
        hops_before = _next_hop_packets(problem, ends) if next_hops else {}
        sent = runtime.exec(
            ends["source"],
            f"python3 -c {shlex.quote(_SWEEP_SEND)} {ends['target_ip']} "
            f"{_SWEEP_PORT} {','.join(map(str, _SWEEP_SOURCES))}",
            timeout=60,
        )
        time.sleep(1.5)
        after = _egress_packets(problem, ends["switch"])
        hops_after = _next_hop_packets(problem, ends) if next_hops else {}
        received = runtime.exec(ends["target"], f"cat {_SWEEP_LOG}", timeout=20)
    except RuntimeError as exc:
        return {"error": f"sweep failed: {exc}"}
    finally:
        runtime.exec(
            ends["target"],
            f"kill $(cat {_SWEEP_PID}) 2>/dev/null; rm -f {_SWEEP_LOG} {_SWEEP_PID}",
            timeout=20,
        )
    if sent.strip() != f"sent {len(_SWEEP_SOURCES)}":
        return {"error": f"sweep sender failed: {sent[:300]}"}
    lines = received.split()
    if received.startswith("[") or not all(line.isdigit() for line in lines):
        return {"error": f"sweep receiver output unparseable: {received[:300]}"}
    ports = [ends["member_port"], *ends["other_ports"], _BAD_PORT]
    lost = sorted(set(_SWEEP_SOURCES) - set(map(int, lines)))
    sample = {
        "flows": len(_SWEEP_SOURCES),
        "delivered": len(_SWEEP_SOURCES) - len(lost),
        "lost_sources": lost,
        "egress_delta": {p: after.get(p, 0) - before.get(p, 0) for p in ports},
    }
    if next_hops:
        sample["next_hop_delta"] = {
            p: hops_after.get(p, 0) - hops_before.get(p, 0) for p in hops_after
        }
    return sample


def _sweep_baseline(
    problem: Any, params: Any, *, next_hops: bool
) -> tuple[bool, dict[str, Any]]:
    """Before inject the sweep arrives and spreads over every group member."""
    ends = _sweep_ends(problem, params)
    problem._audit_sweep = ends
    sample = _sweep(problem, ends, next_hops=next_hops)
    if "error" in sample:
        return False, {**ends, **sample}
    delta = sample.get("next_hop_delta") if next_hops else sample["egress_delta"]
    spread = all(
        delta.get(p, 0) > 0 for p in [ends["member_port"], *ends["other_ports"]]
    )
    delivered = sample["delivered"] >= _SWEEP_MIN_DELIVERED * sample["flows"]
    ok = spread and delivered and sample["egress_delta"][_BAD_PORT] == 0
    return ok, {**ends, **sample, "spread": spread}


def ecmp_sweep_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Every member's next hop receives part of the sweep before inject."""
    return _sweep_baseline(problem, params, next_hops=True)


def selector_sweep_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Every member port carries part of the sweep before inject."""
    return _sweep_baseline(problem, params, next_hops=False)


def _faulted_sweep(
    problem: Any, params: Any, *, next_hops: bool = False
) -> tuple[dict, dict]:
    ends = getattr(problem, "_audit_sweep", None) or _sweep_ends(problem, params)
    return ends, _sweep(problem, ends, next_hops=next_hops)


# A member's next hop that still receives this many sweep packets is in use.
_IDLE_MAX_PACKETS = 2


def ecmp_member_missing(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The removed member's next hop receives none of the sweep; nothing is lost.

    Observed on the next-hop switches: the remaining members' next hops absorb
    every flow while the removed member's healthy next hop sees none.
    """
    ends, sample = _faulted_sweep(problem, params, next_hops=True)
    if "error" in sample:
        return False, {**ends, **sample}
    delta = sample["next_hop_delta"]
    removed_idle = delta.get(ends["member_port"], 0) <= _IDLE_MAX_PACKETS
    control_ok = (
        all(delta.get(p, 0) > _IDLE_MAX_PACKETS for p in ends["other_ports"])
        and sample["delivered"] >= _SWEEP_MIN_DELIVERED * sample["flows"]
    )
    verified = removed_idle and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **ends,
            **sample,
            "removed_next_hop_idle": removed_idle,
            "control_ok": control_ok,
        },
    )


def selector_member_blackhole(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Flows hashed to the bad member leave on the bad port and are lost."""
    ends, sample = _faulted_sweep(problem, params)
    if "error" in sample:
        return False, {**ends, **sample}
    delta = sample["egress_delta"]
    lost = sample["flows"] - sample["delivered"]
    share = sample["flows"] / (1 + len(ends["other_ports"]))
    partial_loss = share / 2 <= lost < sample["flows"]
    bad_port_drops = delta[_BAD_PORT] > 0 and delta[ends["member_port"]] == 0
    control_ok = all(delta[p] > 0 for p in ends["other_ports"])
    verified = partial_loss and bad_port_drops and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **ends,
            **sample,
            "lost": lost,
            "partial_loss": partial_loss,
            "bad_port_drops": bad_port_drops,
            "control_ok": control_ok,
        },
    )


# TEST-NET-3 host route that no P4 scenario addresses or fills.
_PROBE_PREFIX = "203.0.113.77/32"
# BMv2 reports a full table as a target add error rather than RESOURCE_EXHAUSTED.
_TABLE_FULL_CODES = {"RESOURCE_EXHAUSTED"}
_TABLE_FULL_MESSAGE = "Error when adding match entry to target"
_TRY_INSERT = r"""
import json, sys
sys.path.insert(0, "/opt/p4rt")
import grpc
import p4rt_manager as m
from google.rpc import code_pb2, status_pb2
name, prefix, intent_path, p4info_path = sys.argv[1:5]
switch = m.load_json(intent_path)["switches"][name]
index = m.P4InfoIndex(m.load_p4info(p4info_path))
entry = {"prefix": prefix, "group_id": int(switch["ipv4_lpm"][0]["group_id"])}
mode = m.p4runtime_pb2.WriteRequest.CONTINUE_ON_ERROR
client = m.connect(switch)
out = {"prefix": prefix, "inserted": False, "codes": [], "messages": []}
try:
    try:
        client.write([m.lpm_entity(index, entry, insert=True)], mode)
        out["inserted"] = True
    except grpc.RpcError as exc:
        out["codes"].append(exc.code().name)
        for key, value in exc.trailing_metadata() or []:
            if key != "grpc-status-details-bin":
                continue
            status = status_pb2.Status.FromString(value)
            for detail in status.details:
                error = m.p4runtime_pb2.Error()
                detail.Unpack(error)
                out["codes"].append(code_pb2.Code.Name(error.canonical_code))
                out["messages"].append(error.message)
    if out["inserted"]:
        client.write([m.lpm_entity(index, entry, insert=False)], mode)
    lpm = m.observed_switch(client, index)["ipv4_lpm"]
    out["occupancy"] = len(lpm)
    out["present_after"] = any(e.get("prefix") == prefix for e in lpm)
finally:
    client.close()
print(json.dumps(out))
"""


def _try_insert(problem: Any, switch: str) -> dict[str, Any]:
    """Insert and remove one unused LPM entry through the P4Runtime manager."""
    cmd = (
        f"python3 -c {shlex.quote(_TRY_INSERT)} {switch} {_PROBE_PREFIX} "
        f"{INTENT_PATH} {P4INFO_PATH}"
    )
    try:
        out = problem.runtime.exec("fabric_mgr", cmd, timeout=60)
        return json.loads(out.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001 - timeouts and bad output are errors
        return {"error": f"try_insert_failed: {exc}"}


def _transit_ping(problem: Any, intent: dict[str, Any], switch: str) -> dict:
    """Ping from a host on ``switch`` to a remote endpoint it routes by LPM."""
    endpoints = {e["name"]: e["ip"] for e in intent["endpoints"]}
    state = intent["switches"][switch]
    local = [m["peer"] for m in state["members"] if m["peer"] in endpoints]
    routes = [ipaddress.ip_network(e["prefix"]) for e in state["ipv4_lpm"]]
    remote = next(
        ip
        for name, ip in endpoints.items()
        if name not in local and any(ipaddress.ip_address(ip) in net for net in routes)
    )
    stats = ping_stats(problem.runtime, local[0], remote, count=5)
    return {
        "src": local[0],
        "dst": remote,
        "loss_percent": stats.loss_percent,
        "ok": stats.received > 0 and stats.loss_percent <= 20.0,
    }


def _table_write(problem: Any, params: Any) -> dict[str, Any]:
    intent = load_intent(problem.runtime)
    write = _try_insert(problem, params.host_name)
    return {
        "switch": params.host_name,
        "capacity": lpm_capacity(intent),
        "write": write,
        "forwarding": _transit_ping(problem, intent, params.host_name),
    }


def table_exhaustion_baseline(problem: Any, params: Any) -> tuple[bool, dict]:
    """On the healthy switch a new LPM entry installs and is removed again."""
    details = _table_write(problem, params)
    write = details["write"]
    ok = (
        "error" not in write
        and write["inserted"]
        and not write["present_after"]
        and details["forwarding"]["ok"]
    )
    return ok, details


def table_exhaustion(problem: Any, params: Any) -> tuple[bool, dict]:
    """The full LPM table rejects a new entry; installed routes still forward."""
    details = _table_write(problem, params)
    write = details["write"]
    rejected = (
        "error" not in write
        and not write["inserted"]
        and not write["present_after"]
        and (
            bool(_TABLE_FULL_CODES & set(write["codes"]))
            or _TABLE_FULL_MESSAGE in write["messages"]
        )
    )
    at_capacity = "error" not in write and write["occupancy"] >= details["capacity"]
    verified = rejected and at_capacity and details["forwarding"]["ok"]
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={**details, "rejected": rejected, "at_capacity": at_capacity},
    )


# The partial batch leaves two routes on each other's groups: traffic for the
# multi-path route is handed to the local host, and traffic for that host
# loops between the switch and its uplinks until the TTL runs out. The host
# captures inbound only: a backend legitimately sends to the victim address.
_CROSS_PINGS = 10
_CROSS_HTTP_TRIES = 4
_CROSS_LOG = "/tmp/nika-audit-cross.log"
_CROSS_PID = "/tmp/nika-audit-cross.pid"
_CROSS_MAX_LOSS = 10.0
_CROSS_MIN_LOSS = 90.0


def _crossed_reading(problem: Any, params: Any) -> dict[str, Any]:
    """Probe both routes and an untouched one; capture victim traffic on the host."""
    runtime = problem.runtime
    pair = partial_write_routes(load_intent(runtime), params.host_name)
    path = get_probe_path(
        problem.scenario_name or "", topo_size=problem.net_env.model.topo_size
    )
    if path is None or not path.http_url:
        return {**pair, "error": "no_probe_path"}
    started = runtime.exec(
        pair["host"],
        f"rm -f {_CROSS_LOG}; nohup timeout 120 tcpdump -Q in -lni any "
        f"dst host {pair['victim_ip']} >{_CROSS_LOG} 2>/dev/null </dev/null & "
        f"echo $! > {_CROSS_PID}; sleep 1; kill -0 $(cat {_CROSS_PID}) && echo capturing",
        timeout=20,
    )
    try:
        if started.strip() != "capturing":
            return {**pair, "error": f"capture did not start: {started[:300]}"}
        http = [
            http_ok(runtime, path.src_host, path.http_url)
            for _ in range(_CROSS_HTTP_TRIES)
        ]
        victim = ping_stats(
            runtime, pair["sender"], pair["victim_ip"], count=_CROSS_PINGS
        )
        host = ping_stats(runtime, pair["control"], pair["host_ip"], count=_CROSS_PINGS)
        control = ping_stats(
            runtime, pair["sender"], pair["control_ip"], count=_CROSS_PINGS
        )
        time.sleep(1)
        captured = runtime.exec(
            pair["host"],
            f"kill $(cat {_CROSS_PID}) 2>/dev/null; sleep 0.5; grep -c . {_CROSS_LOG}",
            timeout=20,
        )
    finally:
        runtime.exec(pair["host"], f"rm -f {_CROSS_LOG} {_CROSS_PID}", timeout=20)
    if not captured.strip().isdigit():
        return {**pair, "error": f"capture unreadable: {captured[:300]}"}
    return {
        **pair,
        "http_src": path.src_host,
        "http_url": path.http_url,
        "http_ok": sum(http),
        "http_tries": len(http),
        "victim_loss_percent": victim.loss_percent,
        "host_loss_percent": host.loss_percent,
        "control_loss_percent": control.loss_percent,
        "victim_packets_at_host": int(captured.strip()),
    }


def crossed_routes_baseline(problem: Any, params: Any) -> tuple[bool, dict]:
    """Healthy: both routes and the control deliver; the host sees no victim traffic."""
    reading = _crossed_reading(problem, params)
    ok = (
        "error" not in reading
        and reading["http_ok"] == reading["http_tries"]
        and reading["victim_loss_percent"] <= _CROSS_MAX_LOSS
        and reading["host_loss_percent"] <= _CROSS_MAX_LOSS
        and reading["control_loss_percent"] <= _CROSS_MAX_LOSS
        and reading["victim_packets_at_host"] == 0
    )
    return ok, reading


def crossed_routes(problem: Any, params: Any) -> tuple[bool, dict]:
    """Victim traffic lands on the local host, which is itself unreachable.

    A missing route only drops the victim's traffic; here the two routes'
    next hops are exchanged, so the victim's packets arrive at the other
    route's host and that host's own traffic never reaches it. An untouched
    route on the same switch keeps forwarding.
    """
    reading = _crossed_reading(problem, params)
    if "error" in reading:
        return False, reading
    victim_lost = (
        reading["http_ok"] == 0 and reading["victim_loss_percent"] >= _CROSS_MIN_LOSS
    )
    misdelivered = reading["victim_packets_at_host"] > 0
    host_lost = reading["host_loss_percent"] >= _CROSS_MIN_LOSS
    control_ok = reading["control_loss_percent"] <= _CROSS_MAX_LOSS
    verified = victim_lost and misdelivered and host_lost and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **reading,
            "victim_lost": victim_lost,
            "misdelivered": misdelivered,
            "host_lost": host_lost,
            "control_ok": control_ok,
        },
    )
