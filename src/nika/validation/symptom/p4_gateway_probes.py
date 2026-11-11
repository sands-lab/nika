"""P4 gateway telemetry symptom probes (test-path only)."""

from __future__ import annotations

import json
import shlex
import time
from typing import Any

from nika.net_env.p4_dc_gateway.topology_model import INT_REPORTS, VIP_IP
from nika.problems.base import build_verify_result
from nika.traffic.profiles import endpoint_names
from nika.traffic.workload import paused_workload

_REPORTS = INT_REPORTS
# Transfers the kernel already holds drain within a few seconds on BMv2.
_QUIET_SETTLE_SEC = 8.0
_INT_PORT = 19600
_SMALL_PAYLOAD = 200
# 1448 B UDP payload is a 1490 B frame: it fits the default 1500 B INT MTU
# with the 7 B INT-MX header and exceeds an INT MTU of 1480.
_LARGE_PAYLOAD = 1448

_SEND = r"""
import socket, sys, time
dst, port, size = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
for source in map(int, sys.argv[4].split(",")):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", source))
    for _ in range(3):
        sock.sendto(b"i" * size, (dst, port))
        time.sleep(0.02)
    sock.close()
"""


def _clock_ns(problem: Any) -> int:
    """Collector clock; hop reports carry ``packet_timestamp`` from the same host."""
    out = problem.runtime.exec("collector", "date +%s%N", timeout=20)
    return int(out.strip().splitlines()[-1])


def _reports_since(problem: Any, start_ns: int, dst: str, dst_port: int) -> list[dict]:
    """Collector records for ``dst:dst_port`` stamped at or after ``start_ns``.

    The collector trims its file in place, so a line offset taken before a send
    is not stable; timestamps are.
    """
    raw = problem.runtime.exec(
        "collector", f"tail -n 40000 {_REPORTS} 2>/dev/null", timeout=60
    )
    records = []
    for line in raw.splitlines():
        try:
            record = json.loads(line)
            stamp = int(record["packet_timestamp"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if (
            stamp >= start_ns
            and record.get("dst") == dst
            and record.get("dst_port") == dst_port
        ):
            records.append(record)
    return records


def _send(problem: Any, ends: dict, size: int, ports: list[int]) -> list[dict]:
    """Send three datagrams per source port; return the new collector records."""
    start_ns = _clock_ns(problem)
    problem.runtime.exec(
        ends["client"],
        f"python3 -c {shlex.quote(_SEND)} {ends['service_ip']} {_INT_PORT} {size} "
        f"{','.join(map(str, ports))}",
        timeout=60,
    )
    time.sleep(3)
    return _reports_since(problem, start_ns, ends["service_ip"], _INT_PORT)


def _egress_by_source(records: list[dict], device_id: int) -> dict[int, int]:
    egress: dict[int, int] = {}
    for record in records:
        for hop in record.get("hop_sequence") or []:
            if int(hop.get("switch_id", -1)) == device_id:
                egress[int(record["src_port"])] = int(hop["egress_port"])
    return egress


def _int_ends(problem: Any, params: Any) -> dict:
    model = problem.net_env.model
    client = model.client_on_gateway(params.host_name)
    service = model.services[-1]
    return {
        "client": client.name,
        "service": service.name,
        "service_ip": service.ip,
        "device_id": model.switch_info[params.host_name].device_id,
    }


def _large_reports(problem: Any, ends: dict, ports: list[int]) -> int:
    records = _send(problem, ends, _LARGE_PAYLOAD, ports)
    return sum(1 for record in records if int(record["src_port"]) in ports)


def int_headroom_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Find flows that leave on the faulted port; near-MTU packets carry INT."""
    ends = _int_ends(problem, params)
    sources = [20000 + 397 * index for index in range(96)]
    egress = _egress_by_source(
        _send(problem, ends, _SMALL_PAYLOAD, sources), ends["device_id"]
    )
    selected = sorted(p for p, port in egress.items() if port == params.bmv2_port)[:8]
    others = sorted(p for p, port in egress.items() if port != params.bmv2_port)[:8]
    problem._audit_int = {**ends, "selected": selected, "others": others}
    large = _large_reports(problem, ends, selected) if selected else 0
    ok = bool(selected) and bool(others) and large > 0
    return ok, {**problem._audit_int, "large_reports_on_port": large}


def int_headroom(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Near-MTU packets on the faulted port lose INT; small packets keep it."""
    state = getattr(problem, "_audit_int", None) or {}
    selected, others = state.get("selected") or [], state.get("others") or []
    if not selected or not others:
        return False, {"error": "no_flow_selected_the_faulted_port_and_another"}
    large_on_port = _large_reports(problem, state, selected)
    small = _send(problem, state, _SMALL_PAYLOAD, selected)
    small_on_port = sum(1 for record in small if int(record["src_port"]) in selected)
    large_elsewhere = _large_reports(problem, state, others)
    control_ok = large_elsewhere > 0
    verified = large_on_port == 0 and small_on_port > 0 and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **state,
            "large_reports_on_port": large_on_port,
            "small_reports_on_port": small_on_port,
            "large_reports_other_ports": large_elsewhere,
            "control_ok": control_ok,
        },
    )


_LB_FLOWS = 16
_LB_STATUS = "/tmp/probe-lb-flows.status"
_LB_TRIAL_FLOWS = 8
# Observed 5/8 once under the fault (sham 0/8); require most, not all.
_LB_TRIAL_BROKEN_MIN = 6
# Healthy learning takes about 10 ms; the trial waits two orders of magnitude
# longer between opening its connections and the routine pool update.
_LB_TRIAL_LEAD_SEC = 1.0
_LB_TRIAL_SETTLE_SEC = 3.0
# Each connection repeats a keep-alive request every PERIOD seconds for
# DURATION seconds (0: forever) and records whether it is still serving.
_LB_KEEPALIVE = r"""
import http.client, json, os, sys, time
host, count, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
period, duration = float(sys.argv[4]), float(sys.argv[5])
conns = {}
for _ in range(count):
    conn = http.client.HTTPConnection(host, 80, timeout=3)
    conn.connect()
    conns[conn.sock.getsockname()[1]] = conn
opened = time.time()
state = {port: "alive" for port in conns}
while not duration or time.time() < opened + duration:
    for port, conn in conns.items():
        if state[port] != "alive":
            continue
        try:
            conn.request("GET", "/")
            conn.getresponse().read()
        except Exception:
            state[port] = "broken"
    with open(path + ".tmp", "w") as out:
        json.dump({"opened": opened, "flows": state}, out)
    os.replace(path + ".tmp", path)
    time.sleep(period)
"""


def _lb_backends(problem: Any, client_ip: str) -> dict[int, str]:
    """Map client source ports to the backend holding the established socket."""
    owners: dict[int, str] = {}
    for backend in problem.net_env.model.backend_pool:
        out = problem.runtime.exec(
            backend.name,
            f"ss -Htn state established '( sport = :80 )' dst {client_ip}",
            timeout=20,
        )
        for line in out.splitlines():
            peer = line.split()[-1] if line.split() else ""
            if peer.rsplit(":", 1)[-1].isdigit():
                owners[int(peer.rsplit(":", 1)[-1])] = backend.name
    return owners


def _lb_flows(problem: Any, client: str, path: str) -> dict[str, Any]:
    out = problem.runtime.exec(client, f"cat {path} 2>/dev/null", timeout=20)
    try:
        data = json.loads(out)
    except ValueError:
        return {}
    data["flows"] = {int(port): s for port, s in data["flows"].items()}
    return data


def _lb_start(
    problem: Any, client: str, count: int, path: str, period: float, duration: float
) -> dict:
    problem.runtime.exec(
        client,
        f"nohup python3 -c {shlex.quote(_LB_KEEPALIVE)} {VIP_IP} {count} {path} "
        f"{period} {duration} >/dev/null 2>&1 </dev/null &",
        timeout=20,
    )
    flows: dict[str, Any] = {}
    for _ in range(20):
        time.sleep(0.25)
        flows = _lb_flows(problem, client, path)
        if len(flows.get("flows") or {}) == count:
            break
    return flows


def lb_race_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Long-lived VIP connections, learned on both backends before any update."""
    model = problem.net_env.model
    client = model.client_on_gateway(params.host_name)
    flows = _lb_start(problem, client.name, _LB_FLOWS, _LB_STATUS, 1, 0)
    time.sleep(2)
    status = _lb_flows(problem, client.name, _LB_STATUS).get("flows") or {}
    owners = _lb_backends(problem, client.ip)
    problem._audit_lb = {
        "client": client.name,
        "client_ip": client.ip,
        "gateway": params.host_name,
        "device_id": model.switch_info[params.host_name].device_id,
        "owners": owners,
    }
    alive = sum(1 for s in status.values() if s == "alive")
    spread = len(set(owners.get(port) for port in status)) == 2
    # A learner that cannot write ConnTable leaves every connection pending, so
    # the control connections would break on any pool update.
    from nika.net_env.p4_dc_gateway.l4_lb import learner_status

    learner = learner_status(problem.runtime, params.host_name)
    learning = (
        learner.get("learned", 0) >= _LB_FLOWS and learner.get("insert_failures") == 0
    )
    ok = bool(flows) and alive == _LB_FLOWS and spread and learning
    return ok, {**problem._audit_lb, "alive": alive, "learner": learner}


def lb_race(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Connections opened just before a routine pool update break; learned ones do not.

    Each read opens fresh trial connections, waits far longer than healthy
    learning takes, and performs one routine pool update (a VIP version flip
    that moves every bucket to another DIP).  A learned connection keeps its
    DIP; a still-pending one follows the new pool and is reset.  The control is
    the long-lived baseline connections, learned before inject.
    """
    from nika.net_env.p4_dc_gateway.l4_lb import routine_pool_update

    state = getattr(problem, "_audit_lb", None) or {}
    if not state:
        return False, {"error": "no_baseline_connections"}
    path = f"/tmp/probe-lb-trial-{time.time_ns()}.status"
    trial = _lb_start(problem, state["client"], _LB_TRIAL_FLOWS, path, 0.2, 15)
    opened = trial.get("opened")
    if len(trial.get("flows") or {}) != _LB_TRIAL_FLOWS:
        return False, {**state, "error": "trial_connections_not_opened"}
    trial_owners = {
        port: owner
        for port, owner in _lb_backends(problem, state["client_ip"]).items()
        if port in trial["flows"]
    }
    time.sleep(max(0.0, opened + _LB_TRIAL_LEAD_SEC - time.time()))
    update = routine_pool_update(problem.runtime, state["gateway"], state["device_id"])
    if not update.get("ok"):
        return False, {**state, "error": "pool_update_failed", "update": update}
    time.sleep(_LB_TRIAL_SETTLE_SEC)
    trial_flows = _lb_flows(problem, state["client"], path).get("flows") or {}
    control = _lb_flows(problem, state["client"], _LB_STATUS).get("flows") or {}
    broken = sorted(p for p, s in trial_flows.items() if s == "broken")
    control_alive = sum(1 for s in control.values() if s == "alive")
    control_ok = len(control) == _LB_FLOWS and control_alive == _LB_FLOWS
    verified = (
        len(trial_flows) == _LB_TRIAL_FLOWS
        and len(broken) >= _LB_TRIAL_BROKEN_MIN
        and control_ok
    )
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **state,
            "pool_update": update,
            "update_after_open_sec": round(float(update["time"]) - opened, 3),
            "trial_backends": trial_owners,
            "trial_broken": len(broken),
            "trial_total": len(trial_flows),
            "control_alive": control_alive,
            "control_total": len(control),
            "control_ok": control_ok,
        },
    )


# Each hping3 flow leaves one unanswered handshake per source port; ten
# half-open entries is well above the healthy residue of zero or one.
_HALF_OPEN_MIN = 10
_HALF_OPEN_IDLE = 5


def _half_open(problem: Any, node: str, port: int) -> int:
    out = problem.runtime.exec(
        node, f"ss -Htn state syn-recv '( sport = :{port} )' | wc -l", timeout=20
    )
    return int((out.strip().splitlines() or ["0"])[-1])


def _syn_ends(problem: Any, params: Any) -> dict | None:
    services = problem.net_env.model.services
    target = next((s for s in services if s.ip == params.target_ip), None)
    control = next((s for s in services if s is not target), None)
    if target is None or control is None:
        return None
    return {"target": target.name, "control": control.name}


def _syn_sample(problem: Any, params: Any) -> dict | None:
    ends = _syn_ends(problem, params)
    if ends is None:
        return None
    return {
        **ends,
        "target_half_open": _half_open(problem, ends["target"], params.target_port),
        "control_half_open": _half_open(problem, ends["control"], params.target_port),
    }


def syn_flood_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    sample = _syn_sample(problem, params)
    if sample is None:
        return False, {"error": "target_ip_is_not_a_service"}
    ok = sample["target_half_open"] < _HALF_OPEN_IDLE
    return ok, sample


def syn_flood(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Flood SYNs pile up half-open handshakes on the target listener only."""
    sample = _syn_sample(problem, params)
    if sample is None:
        return False, {"error": "target_ip_is_not_a_service"}
    control_ok = sample["control_half_open"] < _HALF_OPEN_IDLE
    verified = sample["target_half_open"] >= _HALF_OPEN_MIN and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={**sample, "control_ok": control_ok},
    )


# Single-datagram flows with distinct source ports; INT maps each port to its
# egress on the faulted switch before inject. Flows on the faulted port and on
# the other ports share one sending window, so transient BMv2 loss hits both.
# A logging BMv2 sustains about 100 pps of INT-encapsulated traffic.
_LOSS_FLOWS = 2048
_LOSS_PORT = 19407
_LOSS_LOG = "/tmp/probe-udp-loss.log"
_LOSS_LISTEN = rf"""
import socket
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", {_LOSS_PORT}))
log = open({_LOSS_LOG!r}, "a", buffering=1)
while True:
    data, _ = sock.recvfrom(2048)
    log.write(data.decode("ascii") + "\n")
"""
_LOSS_SEND = r"""
import socket, sys, time
dst, port, label, count = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
for index in range(count):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 20000 + index))
    sock.sendto(f"{label}:{20000 + index}".encode(), (dst, port))
    sock.close()
    time.sleep(0.01)
"""


def _loss_round(problem: Any, state: dict) -> set[int]:
    """Send every flow once; return the source ports that reached the service."""
    label = f"l{time.monotonic_ns()}"
    problem.runtime.exec(
        state["client"],
        f"python3 -c {shlex.quote(_LOSS_SEND)} {state['service_ip']} {_LOSS_PORT} "
        f"{label} {_LOSS_FLOWS}",
        timeout=120,
    )
    time.sleep(1.5)
    out = problem.runtime.exec(
        state["service"], f"grep '^{label}:' {_LOSS_LOG} || true", timeout=30
    )
    return {int(line.split(":")[1]) for line in out.splitlines() if ":" in line}


def silent_loss_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Map flows to the switch egress; every flow arrives before inject."""
    model = problem.net_env.model
    switch = params.host_name
    client = model.client_on_gateway(switch) if switch in model.gateways else None
    client = client or model.clients[0]
    service = model.services[0]
    state = {"client": client.name, "service": service.name, "service_ip": service.ip}
    problem.runtime.exec(
        service.name,
        f"nohup python3 -u -c {shlex.quote(_LOSS_LISTEN)} >/dev/null 2>&1 </dev/null &",
        timeout=20,
    )
    time.sleep(1)
    start_ns = _clock_ns(problem)
    arrived = _loss_round(problem, state)
    time.sleep(2)
    records = _reports_since(problem, start_ns, service.ip, _LOSS_PORT)
    egress = _egress_by_source(records, model.switch_info[switch].device_id)
    state["on_port"] = sorted(p for p, out in egress.items() if out == params.bmv2_port)
    state["elsewhere"] = sorted(
        p for p, out in egress.items() if out != params.bmv2_port
    )
    problem._audit_loss = state
    ok = (
        bool(state["on_port"])
        and bool(state["elsewhere"])
        and len(arrived) >= 0.99 * _LOSS_FLOWS
    )
    return ok, {
        **{k: v for k, v in state.items() if k not in ("on_port", "elsewhere")},
        "flows_on_port": len(state["on_port"]),
        "flows_elsewhere": len(state["elsewhere"]),
        "arrived": len(arrived),
    }


def silent_loss(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Flows on the faulted port lose packets; flows on other ports do not."""
    state = getattr(problem, "_audit_loss", None) or {}
    if not state.get("on_port") or not state.get("elsewhere"):
        return False, {"error": "no_flow_selected_the_port_and_another"}
    arrived = _loss_round(problem, state)
    on_port, elsewhere = set(state["on_port"]), set(state["elsewhere"])
    port_loss = 100 * len(on_port - arrived) / len(on_port)
    other_loss = 100 * len(elsewhere - arrived) / len(elsewhere)
    verified = port_loss >= 0.5 and port_loss - other_loss >= 0.5
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "client": state["client"],
            "service": state["service"],
            "flows_on_port": len(on_port),
            "flows_elsewhere": len(elsewhere),
            "port_loss_percent": port_loss,
            "other_loss_percent": other_loss,
            "control_ok": bool(elsewhere) and other_loss < port_loss,
        },
    )


def _sink_ports(records: list[dict]) -> set[int]:
    return {int(r["src_port"]) for r in records if r.get("sink_seen")}


def tcam_drop_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Find flows to the target that cross the switch; all of them arrive."""
    model = problem.net_env.model
    switch = params.host_name
    on_gateway = switch in model.gateways
    source = model.client_on_gateway(switch) if on_gateway else model.clients[0]
    ends = {"client": source.name, "service_ip": params.target_ip}
    sources = [10007 + 1117 * i for i in range(48)]
    records = _send(problem, ends, _SMALL_PAYLOAD, sources)
    egress = _egress_by_source(records, model.switch_info[switch].device_id)
    crossing = sorted(egress)
    others = sorted(set(sources) - set(egress))[:16]
    control = ends
    if on_gateway:
        # The declared control client, unless it sits behind the faulted gateway.
        peer = next(
            c
            for c in sorted(
                model.clients, key=lambda c: c.name != params.control_source
            )
            if c.attached_switch != switch
        )
        control = {"client": peer.name, "service_ip": params.target_ip}
        others = sources[:16]
    problem._audit_tcam = {
        "path": ends,
        "control": control,
        "crossing": crossing,
        "others": others,
    }
    arrived = _sink_ports(records)
    ok = bool(crossing) and set(crossing) <= arrived
    return ok, {**problem._audit_tcam, "crossing_arrived": len(arrived & set(crossing))}


def tcam_drop(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Flows through the corrupt entry never reach a leaf; other flows still do."""
    state = getattr(problem, "_audit_tcam", None) or {}
    if not state.get("crossing"):
        return False, {"error": "no_flow_crossed_the_switch"}
    crossing = state["crossing"]
    lost = set(crossing) - _sink_ports(
        _send(problem, state["path"], _SMALL_PAYLOAD, crossing)
    )
    others = state["others"]
    kept = _sink_ports(_send(problem, state["control"], _SMALL_PAYLOAD, others))
    control_ok = bool(others) and len(kept & set(others)) >= len(others) * 0.9
    verified = lost == set(crossing) and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **state,
            "crossing_lost": len(lost),
            "control_arrived": len(kept & set(others)),
            "control_ok": control_ok,
        },
    )


# Clients send 100 pps of ECT(0) datagrams through the probed port, above the
# ~61 pps the gateway's virtual queue drains and below the ~120 pps of INT
# traffic the fabric forwards. Before and after that port the traffic is split
# over at least two ports (~50 pps each), so only the probed port queues and
# marks CE. The ECMP hash covers the destination port, so the burst reuses the
# port the INT path selection probed.
_ECN_PPS = 100
_ECN_SECONDS = 4
_ECN_STATUS = "/tmp/probe-ecn.json"
_ECN_BURST = r"""
import socket, sys, time
flows = [f.split(":") for f in sys.argv[1].split(",")]
port, pps, seconds = int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
start_at = float(sys.argv[5])
socks = []
for dst, source in flows:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 2)
    sock.bind(("0.0.0.0", int(source)))
    socks.append((sock, dst))
time.sleep(max(0.0, start_at - time.time()))
start, sent = time.time(), 0
while time.time() - start < seconds:
    sock, dst = socks[sent % len(socks)]
    sock.sendto(b"e" * 64, (dst, port))
    sent += 1
    time.sleep(max(0.0, start + sent / pps - time.time()))
"""
_ECN_LISTEN = r"""
import json, os, socket, sys, time
port, seconds, path = int(sys.argv[1]), float(sys.argv[2]), sys.argv[3]
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.IPPROTO_IP, socket.IP_RECVTOS, 1)
sock.bind(("0.0.0.0", port))
sock.settimeout(0.5)
total = ce = 0
end = time.time() + seconds
while time.time() < end:
    try:
        _data, ancillary, _flags, _addr = sock.recvmsg(2048, 64)
    except socket.timeout:
        continue
    total += 1
    ce += any(data[:1] and data[0] & 3 == 3 for _level, _type, data in ancillary)
with open(path + ".tmp", "w") as out:
    json.dump({"total": total, "ce": ce}, out)
os.replace(path + ".tmp", path)
"""


def _ecn_ports(problem: Any, switch: str, bmv2_port: int) -> dict | None:
    """Return the probed port and a sibling port facing the same kind of peer."""
    model = problem.net_env.model
    ports = model.ports[switch]
    probed = next((p for p in ports if p.bmv2_port == bmv2_port), None)
    if probed is None or probed.role not in ("spine", "leaf"):
        return None
    sibling = next(
        (p for p in ports if p.role == probed.role and p is not probed), None
    )
    return {"probed": probed, "sibling": sibling}


def _ecn_flows(problem: Any, switch: str, port: Any) -> dict[str, list[str]]:
    """Per client, ``service_ip:source_port`` flows that leave ``switch`` on ``port``."""
    model = problem.net_env.model
    if port.role == "spine":
        clients = [model.client_on_gateway(switch)]
        first_per_leaf = {s.attached_switch: s for s in reversed(model.services)}
        services = list(first_per_leaf.values())
    else:
        clients = model.clients
        services = [s for s in model.services if s.attached_switch == port.peer]
    device_id = model.switch_info[switch].device_id
    flows: dict[str, list[str]] = {}
    # The CRC16 ECMP selector is linear: source ports that differ only in low
    # bits reach a subset of the members, so spread them over the port range.
    sources = [10007 + 1117 * i for i in range(48)]
    for client in clients:
        for service in services:
            ends = {"client": client.name, "service_ip": service.ip}
            egress = _egress_by_source(
                _send(problem, ends, _SMALL_PAYLOAD, sources), device_id
            )
            flows.setdefault(client.name, []).extend(
                f"{service.ip}:{p}"
                for p, out in sorted(egress.items())
                if out == port.bmv2_port
            )
    return {client: found for client, found in flows.items() if found}


def _ecn_burst(problem: Any, flows: dict[str, list[str]]) -> dict[str, int]:
    """Send the converging burst; return datagrams and CE marks at the services.

    A virtual queue drains about 61 packets per second, so the scenario
    workload alone keeps some downstream queues marking; it holds still while
    the burst runs and the marks come from the probed path only.
    """
    with paused_workload(
        problem.runtime,
        problem.net_env.business_workload(),
        _QUIET_SETTLE_SEC,
        hosts=endpoint_names(problem.net_env),
    ):
        return _ecn_burst_counted(problem, flows)


def _ecn_burst_counted(problem: Any, flows: dict[str, list[str]]) -> dict[str, int]:
    services = {flow.split(":")[0] for found in flows.values() for flow in found}
    receivers = [s.name for s in problem.net_env.model.services if s.ip in services]
    # Senders start in separate execs; a shared start time keeps their bursts
    # overlapping on the probed port.
    lead = 2.0 + len(flows)
    listen = lead + _ECN_SECONDS + 4
    for name in receivers:
        problem.runtime.exec(
            name,
            f"rm -f {_ECN_STATUS}; nohup python3 -c {shlex.quote(_ECN_LISTEN)} "
            f"{_INT_PORT} {listen} {_ECN_STATUS} >/dev/null 2>&1 </dev/null &",
            timeout=20,
        )
    time.sleep(1)
    pps = _ECN_PPS / len(flows)
    start_at = time.time() + lead
    for client, found in flows.items():
        problem.runtime.exec(
            client,
            f"nohup python3 -c {shlex.quote(_ECN_BURST)} {','.join(found)} "
            f"{_INT_PORT} {pps} {_ECN_SECONDS} {start_at} "
            ">/dev/null 2>&1 </dev/null &",
            timeout=20,
        )
    time.sleep(max(0.0, start_at - time.time()) + _ECN_SECONDS + 5)
    counts = {"total": 0, "ce": 0}
    for name in receivers:
        out = problem.runtime.exec(name, f"cat {_ECN_STATUS} 2>/dev/null", timeout=20)
        try:
            sample = json.loads(out.strip() or "{}")
        except json.JSONDecodeError:
            sample = {}
        counts["total"] += int(sample.get("total", 0))
        counts["ce"] += int(sample.get("ce", 0))
    return counts


def ecn_marking_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """A burst that overruns the probed port gets CE marks there before inject."""
    ports = _ecn_ports(problem, params.host_name, params.bmv2_port)
    if ports is None or ports["sibling"] is None:
        return False, {"error": "port_has_no_downstream_sibling"}
    flows = _ecn_flows(problem, params.host_name, ports["probed"])
    control = _ecn_flows(problem, params.host_name, ports["sibling"])
    problem._audit_ecn = {"flows": flows, "control_flows": control}
    if not flows or not control:
        return False, {**problem._audit_ecn, "error": "no_flow_selected_the_port"}
    counts = _ecn_burst(problem, flows)
    ok = counts["total"] > 0 and counts["ce"] > 0
    return ok, {"probed": counts, **problem._audit_ecn}


def ecn_marking(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The same burst passes the probed port unmarked; a sibling port still marks."""
    state = getattr(problem, "_audit_ecn", None) or {}
    if not state.get("flows") or not state.get("control_flows"):
        return False, {"error": "no_baseline_flows"}
    probed = _ecn_burst(problem, state["flows"])
    control = _ecn_burst(problem, state["control_flows"])
    control_ok = control["ce"] > 0
    verified = probed["total"] > 0 and probed["ce"] == 0 and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "probed": probed,
            "control": control,
            "control_ok": control_ok,
            **state,
        },
    )
