"""Symptom probes for P4Runtime forwarding-state faults on BMv2."""

from __future__ import annotations

import ipaddress
import json
import shlex
import time
from typing import Any

from nika.net_env.p4_dc_fabric.fabric_manager.apply import INTENT_PATH, P4INFO_PATH
from nika.net_env.verify import ping_stats
from nika.problems.base import build_verify_result
from nika.problems.forwarding_encapsulation_policy.p4_runtime import _BAD_PORT
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
    }


def _egress_packets(problem: Any, switch: str) -> dict[int, int]:
    observed = run_manager(problem.runtime, "counters", "--switch", switch, timeout=60)
    counters = (observed.get("counters") or {}).get(switch)
    if not observed.get("ok") or counters is None:
        raise RuntimeError(f"counter read failed on {switch}: {observed}")
    return {
        int(port): int(value["packets"])
        for port, value in (counters.get("egress") or {}).items()
    }


def _sweep(problem: Any, ends: dict[str, Any]) -> dict[str, Any]:
    """Send every flow once; return delivered flows and per-port egress deltas."""
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
        sent = runtime.exec(
            ends["source"],
            f"python3 -c {shlex.quote(_SWEEP_SEND)} {ends['target_ip']} "
            f"{_SWEEP_PORT} {','.join(map(str, _SWEEP_SOURCES))}",
            timeout=60,
        )
        time.sleep(1.5)
        after = _egress_packets(problem, ends["switch"])
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
    return {
        "flows": len(_SWEEP_SOURCES),
        "delivered": len(_SWEEP_SOURCES) - len(lost),
        "lost_sources": lost,
        "egress_delta": {p: after.get(p, 0) - before.get(p, 0) for p in ports},
    }


def ecmp_sweep_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Before inject the sweep arrives and spreads over every group member."""
    ends = _sweep_ends(problem, params)
    problem._audit_sweep = ends
    sample = _sweep(problem, ends)
    if "error" in sample:
        return False, {**ends, **sample}
    delta = sample["egress_delta"]
    spread = all(delta[p] > 0 for p in [ends["member_port"], *ends["other_ports"]])
    delivered = sample["delivered"] >= _SWEEP_MIN_DELIVERED * sample["flows"]
    ok = spread and delivered and delta[_BAD_PORT] == 0
    return ok, {**ends, **sample, "spread": spread}


def _faulted_sweep(problem: Any, params: Any) -> tuple[dict, dict]:
    ends = getattr(problem, "_audit_sweep", None) or _sweep_ends(problem, params)
    return ends, _sweep(problem, ends)


def ecmp_member_missing(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The removed member's uplink carries no flow; the others deliver all."""
    ends, sample = _faulted_sweep(problem, params)
    if "error" in sample:
        return False, {**ends, **sample}
    delta = sample["egress_delta"]
    removed_idle = delta[ends["member_port"]] == 0
    control_ok = (
        all(delta[p] > 0 for p in ends["other_ports"])
        and sample["delivered"] >= _SWEEP_MIN_DELIVERED * sample["flows"]
    )
    verified = removed_idle and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **ends,
            **sample,
            "removed_member_idle": removed_idle,
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
sys.path.insert(0, "/opt/nika")
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
