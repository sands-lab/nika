"""P4 gateway telemetry symptom probes (test-path only)."""

from __future__ import annotations

import json
import shlex
import time
from typing import Any

from nika.net_env.p4_dc_gateway.topology_model import VIP_IP
from nika.problems.base import build_verify_result

_REPORTS = "/var/lib/nika/int_reports.jsonl"
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


def _report_count(problem: Any) -> int:
    out = problem.runtime.exec(
        "collector", f"cat {_REPORTS} 2>/dev/null | wc -l", timeout=20
    )
    return int((out.strip().splitlines() or ["0"])[-1])


def _send(problem: Any, ends: dict, size: int, ports: list[int]) -> list[dict]:
    """Send three datagrams per source port; return the new collector records."""
    mark = _report_count(problem)
    problem.runtime.exec(
        ends["client"],
        f"python3 -c {shlex.quote(_SEND)} {ends['service_ip']} {_INT_PORT} {size} "
        f"{','.join(map(str, ports))}",
        timeout=60,
    )
    time.sleep(3)
    raw = problem.runtime.exec(
        "collector", f"tail -n +{mark + 1} {_REPORTS} 2>/dev/null", timeout=30
    )
    records = []
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            record.get("dst") == ends["service_ip"]
            and record.get("dst_port") == _INT_PORT
        ):
            records.append(record)
    return records


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
    ok = bool(selected) and large > 0
    return ok, {**problem._audit_int, "large_reports_on_port": large}


def int_headroom(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Near-MTU packets on the faulted port lose INT; small packets keep it."""
    state = getattr(problem, "_audit_int", None) or {}
    selected, others = state.get("selected") or [], state.get("others") or []
    if not selected:
        return False, {"error": "no_flow_selected_the_faulted_port"}
    large_on_port = _large_reports(problem, state, selected)
    small = _send(problem, state, _SMALL_PAYLOAD, selected)
    small_on_port = sum(1 for record in small if int(record["src_port"]) in selected)
    large_elsewhere = _large_reports(problem, state, others) if others else None
    control_ok = large_elsewhere is None or large_elsewhere > 0
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
_LB_STATUS = "/tmp/nika-audit-lb-flows.status"
# Each connection repeats a keep-alive request once a second and records its
# local port and whether the connection is still serving.
_LB_KEEPALIVE = r"""
import http.client, os, sys, time
host, count, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
conns = {}
for _ in range(count):
    conn = http.client.HTTPConnection(host, 80, timeout=3)
    conn.connect()
    conns[conn.sock.getsockname()[1]] = conn
state = {port: "alive" for port in conns}
while True:
    for port, conn in conns.items():
        if state[port] != "alive":
            continue
        try:
            conn.request("GET", "/")
            conn.getresponse().read()
        except Exception:
            state[port] = "broken"
    with open(path + ".tmp", "w") as out:
        out.write("".join(f"{p} {s}\n" for p, s in state.items()))
    os.replace(path + ".tmp", path)
    time.sleep(1)
"""
# New connections opened after the update, held long enough to be listed.
_LB_HOLD = r"""
import http.client, sys, time
host = sys.argv[1]
conns = [http.client.HTTPConnection(host, 80, timeout=3) for _ in range(8)]
for conn in conns:
    conn.connect()
time.sleep(6)
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


def _lb_status(problem: Any, client: str) -> dict[int, str]:
    out = problem.runtime.exec(client, f"cat {_LB_STATUS} 2>/dev/null", timeout=20)
    return {
        int(port): status
        for port, status in (line.split() for line in out.splitlines() if line.strip())
    }


def lb_race_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Long-lived VIP connections spread over both backends before the update."""
    model = problem.net_env.model
    client = model.client_on_gateway(params.host_name)
    problem.runtime.exec(
        client.name,
        f"nohup python3 -c {shlex.quote(_LB_KEEPALIVE)} {VIP_IP} {_LB_FLOWS} "
        f"{_LB_STATUS} >/tmp/nika-audit-lb-flows.log 2>&1 </dev/null &",
        timeout=20,
    )
    status: dict[int, str] = {}
    for _ in range(10):
        time.sleep(1)
        status = _lb_status(problem, client.name)
        if len(status) == _LB_FLOWS:
            break
    owners = _lb_backends(problem, client.ip)
    problem._audit_lb = {
        "client": client.name,
        "client_ip": client.ip,
        "owners": owners,
    }
    alive = sum(1 for s in status.values() if s == "alive")
    spread = len(set(owners.get(port) for port in status)) == 2
    ok = alive == _LB_FLOWS and spread
    return ok, {**problem._audit_lb, "alive": alive}


def lb_race(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Unlearned connections move to the new pool: flows on the old DIP break."""
    state = getattr(problem, "_audit_lb", None) or {}
    if not state:
        return False, {"error": "no_baseline_connections"}
    old_dip, new_dip = (b.name for b in problem.net_env.model.backend_pool[:2])
    time.sleep(3)
    status = _lb_status(problem, state["client"])
    owners = state["owners"]
    old = [s for p, s in status.items() if owners.get(p) == old_dip]
    kept = [s for p, s in status.items() if owners.get(p) == new_dip]
    problem.runtime.exec(
        state["client"],
        f"nohup python3 -c {shlex.quote(_LB_HOLD)} {VIP_IP} >/dev/null 2>&1 </dev/null &",
        timeout=20,
    )
    time.sleep(1.5)
    new_owners = {
        port: owner
        for port, owner in _lb_backends(problem, state["client_ip"]).items()
        if port not in status
    }
    old_broken = bool(old) and all(s == "broken" for s in old)
    control_ok = bool(kept) and all(s == "alive" for s in kept)
    new_on_new = bool(new_owners) and set(new_owners.values()) == {new_dip}
    verified = old_broken and control_ok and new_on_new
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **state,
            "old_backend": old_dip,
            "new_backend": new_dip,
            "old_backend_flows": old,
            "new_backend_flows": kept,
            "new_connection_backends": new_owners,
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
_LOSS_LOG = "/tmp/nika-audit-udp-loss.log"
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
    mark = _report_count(problem)
    arrived = _loss_round(problem, state)
    time.sleep(2)
    raw = problem.runtime.exec(
        "collector", f"tail -n +{mark + 1} {_REPORTS} 2>/dev/null", timeout=60
    )
    records = []
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("dst") == service.ip and record.get("dst_port") == _LOSS_PORT:
            records.append(record)
    egress = _egress_by_source(records, model.switch_info[switch].device_id)
    state["on_port"] = sorted(p for p, out in egress.items() if out == params.bmv2_port)
    state["elsewhere"] = sorted(
        p for p, out in egress.items() if out != params.bmv2_port
    )
    problem._audit_loss = state
    ok = bool(state["on_port"]) and len(arrived) >= 0.99 * _LOSS_FLOWS
    return ok, {
        **{k: v for k, v in state.items() if k not in ("on_port", "elsewhere")},
        "flows_on_port": len(state["on_port"]),
        "flows_elsewhere": len(state["elsewhere"]),
        "arrived": len(arrived),
    }


def silent_loss(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Flows on the faulted port lose packets; flows on other ports do not."""
    state = getattr(problem, "_audit_loss", None) or {}
    if not state.get("on_port"):
        return False, {"error": "no_flow_selected_the_port"}
    arrived = _loss_round(problem, state)
    on_port, elsewhere = set(state["on_port"]), set(state["elsewhere"])
    port_loss = 100 * len(on_port - arrived) / len(on_port)
    other_loss = 100 * len(elsewhere - arrived) / len(elsewhere) if elsewhere else 0.0
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
        peer = next(c for c in model.clients if c.attached_switch != switch)
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
_ECN_STATUS = "/tmp/nika-audit-ecn.json"
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
    """Send the converging burst; return datagrams and CE marks at the services."""
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
