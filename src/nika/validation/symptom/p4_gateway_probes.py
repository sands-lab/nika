"""P4 gateway telemetry symptom probes (test-path only)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import shlex
import time
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

from nika.net_env.p4_dc_gateway.apply import QUEUE_RATE_PPS
from nika.net_env.p4_dc_gateway.topology_model import INT_REPORTS, VIP_IP
from nika.problems.base import build_verify_result
from nika.problems.support.polling import wait_until
from nika.problems.security.tcp_syn_flood import (
    parse_syn_queue_read,
    syn_queue_pressure,
    syn_queue_read_command,
)
from nika.service.lab.checked_exec import checked_exec
from nika.traffic.profiles import endpoint_names
from nika.traffic.workload import paused_workload

_REPORTS = INT_REPORTS
# Transfers the kernel already holds drain within a few seconds on BMv2.
_QUIET_SETTLE_SEC = 8.0
_INT_PORT = 19600
_SMALL_PAYLOAD = 200

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
    # Collector timestamps describe receipt, so a delayed report from the
    # preceding client can arrive in this window with the same source port.
    return [
        record
        for record in _reports_since(problem, start_ns, ends["service_ip"], _INT_PORT)
        if record.get("src") == ends["client_ip"]
    ]


def _egress_by_source(records: list[dict], device_id: int) -> dict[int, int]:
    egress: dict[int, int] = {}
    for record in records:
        for hop in record.get("hop_sequence") or []:
            if int(hop.get("switch_id", -1)) == device_id:
                egress[int(record["src_port"])] = int(hop["egress_port"])
    return egress


_LB_FLOWS = 16
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
from concurrent.futures import ThreadPoolExecutor
host, count, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
period, duration = float(sys.argv[4]), float(sys.argv[5])
for attempt in range(3):
    conns = {}
    conn = None
    try:
        for _ in range(count):
            # Reserve the local port across destinations before VIP translation.
            conn = http.client.HTTPConnection(host, 80, timeout=3, source_address=("", 0))
            conn.connect()
            conn.sock.settimeout(10)
            conns[conn.sock.getsockname()[1]] = conn
    except OSError:
        if conn is not None:
            conn.close()
        for active in conns.values():
            active.close()
        if attempt == 2:
            raise
    else:
        break
opened = time.time()
state = {port: "alive" for port in conns}
def request(item):
    port, conn = item
    if state[port] != "alive":
        return
    try:
        conn.request("GET", "/")
        conn.getresponse().read()
        if conn.sock is None or conn.sock.getsockname()[1] != port:
            state[port] = "broken"
            conn.close()
    except Exception as exc:
        state[port] = "broken"
        conn.close()
        print(repr(exc), file=sys.stderr, flush=True)
try:
    with ThreadPoolExecutor(max_workers=count) as pool:
        while True:
            list(pool.map(request, conns.items()))
            with open(path + ".tmp", "w") as out:
                json.dump({"opened": opened, "flows": state}, out)
            os.replace(path + ".tmp", path)
            if duration and time.time() >= opened + duration:
                break
            time.sleep(period)
finally:
    for conn in conns.values():
        conn.close()
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
        f"{period} {duration} >/dev/null 2>&1 </dev/null & echo $! > {path}.pid",
        timeout=20,
    )
    flows: dict[str, Any] = {}
    # Three setup attempts allow three seconds per connection. The first
    # concurrent HTTP round has a ten-second deadline.
    deadline = time.monotonic() + count * 9 + 10
    while time.monotonic() < deadline:
        time.sleep(0.25)
        flows = _lb_flows(problem, client, path)
        if len(flows.get("flows") or {}) == count:
            break
    return flows


_LB_BUSINESS_READ = r"""
import json, re, subprocess, sys
pids = subprocess.run(["pgrep", "-f", "[/]usr/local/bin/vip-session"],
                      capture_output=True, text=True).stdout.split()
raw = subprocess.check_output(["ss", "-HtnpiO", "state", "established", "dst", sys.argv[1]], text=True)
flows = {}
for line in raw.splitlines():
    if not any("pid=" + pid + "," in line for pid in pids):
        continue
    fields = line.split()
    match = re.search(r"bytes_received:(\d+)", line)
    if len(fields) >= 4 and match:
        flows[int(fields[2].rsplit(":", 1)[1])] = int(match[1])
print(json.dumps(flows))
"""


def _lb_business_flows(problem: Any, client: str) -> dict[int, int]:
    """Read the ordinary browsing workload's established VIP sockets."""
    out = problem.runtime.exec(
        client,
        f"python3 -c {shlex.quote(_LB_BUSINESS_READ)} {VIP_IP}",
        timeout=20,
    )
    return {int(port): int(received) for port, received in json.loads(out).items()}


def lb_race_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Retain sixteen ordinary browsing connections learned before injection."""
    from nika.net_env.p4_dc_gateway.l4_lb import learner_status

    model = problem.net_env.model
    client = model.client_on_gateway(params.host_name)
    flows: dict[int, int] = {}

    def ready() -> bool:
        nonlocal flows
        flows = _lb_business_flows(problem, client.name)
        return len(flows) == _LB_FLOWS

    initial_ready = wait_until(ready, 120, interval_s=1)
    owners = {
        port: owner
        for port, owner in _lb_backends(problem, client.ip).items()
        if port in flows
    }
    process = next(
        p
        for p in problem.net_env.business_workload().processes
        if p.node == client.name and p.command == "vip-session"
    )
    pause = process.args[process.args.index("--pause") + 1]
    # The next ordinary request may wait for the declared browsing interval.
    control_wait_sec = float(pause.split("-")[-1]) + 10

    problem._audit_lb = {
        "client": client.name,
        "client_ip": client.ip,
        "gateway": params.host_name,
        "device_id": model.switch_info[params.host_name].device_id,
        "owners": owners,
        "control_wait_sec": control_wait_sec,
    }
    learner = learner_status(problem.runtime, params.host_name)
    learning = (
        learner.get("learned", 0) >= _LB_FLOWS and learner.get("insert_failures") == 0
    )
    ok = (
        initial_ready
        and len(owners) == _LB_FLOWS
        and len(set(owners.values())) == 2
        and learning
    )
    return ok, {
        **problem._audit_lb,
        "initial_reading_ready": initial_ready,
        "alive": len(flows),
        "learner": learner,
    }


def _stop_file_probe(problem: Any, node: str, path: str) -> None:
    """Stop the owned finite receiver and remove its measurement files."""
    problem.runtime.exec(
        node,
        f"if test -f {path}.pid; then kill $(cat {path}.pid) 2>/dev/null || true; fi; "
        f"rm -f {path} {path}.tmp {path}.pid",
        timeout=20,
    )


@contextmanager
def _lb_trial(problem: Any, client: str, path: str):
    """Own one finite trial and remove its process and files on every exit."""
    try:
        yield _lb_start(problem, client, _LB_TRIAL_FLOWS, path, 0.2, 30)
    finally:
        _stop_file_probe(problem, client, path)


def lb_race(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Measure the controlled pool update after workload queues drain."""
    workload = problem.net_env.business_workload()
    # Ordinary long-lived VIP browsing supplies the control connections.
    quiet = replace(
        workload,
        processes=tuple(p for p in workload.processes if p.command != "vip-session"),
    )
    with paused_workload(
        problem.runtime, quiet, _QUIET_SETTLE_SEC, hosts=endpoint_names(problem.net_env)
    ):
        return _lb_race_quiet(problem, params)


def _lb_race_quiet(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Connections opened just before a routine pool update break; learned ones do not.

    Each read opens fresh trial connections, waits far longer than healthy
    learning takes, and performs one routine pool update (a VIP version flip
    that moves every bucket to another DIP).  A learned connection keeps its
    DIP; a still-pending one follows the new pool and is reset.  The control is
    the long-lived baseline connections, learned before inject.
    """
    state = getattr(problem, "_audit_lb", None) or {}
    if not state:
        return False, {"error": "no_baseline_connections"}
    path = f"/tmp/probe-lb-trial-{time.time_ns()}.status"
    with _lb_trial(problem, state["client"], path) as trial:
        return _lb_race_trial(problem, state, path, trial)


def _lb_race_trial(
    problem: Any, state: dict, path: str, trial: dict
) -> tuple[bool, dict]:
    from nika.net_env.p4_dc_gateway.l4_lb import routine_pool_update

    opened = trial.get("opened")
    if len(trial.get("flows") or {}) != _LB_TRIAL_FLOWS:
        return False, {**state, "error": "trial_connections_not_opened"}
    if any(status != "alive" for status in trial["flows"].values()):
        return False, {**state, "error": "trial_connections_not_alive"}
    trial_owners = {
        port: owner
        for port, owner in _lb_backends(problem, state["client_ip"]).items()
        if port in trial["flows"]
    }
    time.sleep(max(0.0, opened + _LB_TRIAL_LEAD_SEC - time.time()))
    before = _lb_business_flows(problem, state["client"])
    update = routine_pool_update(problem.runtime, state["gateway"], state["device_id"])
    if not update.get("ok"):
        return False, {**state, "error": "pool_update_failed", "update": update}
    time.sleep(_LB_TRIAL_SETTLE_SEC)
    trial_flows = _lb_flows(problem, state["client"], path).get("flows") or {}
    control = _lb_business_flows(problem, state["client"])

    def controls_progressed():
        nonlocal control
        control = _lb_business_flows(problem, state["client"])
        return all(
            control.get(port, 0) > before.get(port, 0) for port in state["owners"]
        )

    progress = wait_until(controls_progressed, state["control_wait_sec"], interval_s=1)
    owners = _lb_backends(problem, state["client_ip"])
    broken = sorted(p for p, s in trial_flows.items() if s == "broken")
    control_alive = sum(
        port in control and owners.get(port) == owner
        for port, owner in state["owners"].items()
    )
    control_ok = (
        len(state["owners"]) == _LB_FLOWS
        and set(before) == set(state["owners"])
        and control_alive == _LB_FLOWS
        and progress
    )
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


_HALF_OPEN_IDLE = 5


def _syn_queue(problem: Any, node: str, port: int) -> dict[str, int]:
    out = problem.runtime.exec(node, syn_queue_read_command(port), timeout=20)
    return parse_syn_queue_read(out)


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
    target = _syn_queue(problem, ends["target"], params.target_port)
    control = _syn_queue(problem, ends["control"], params.target_port)
    return {
        **ends,
        **{f"target_{key}": value for key, value in target.items()},
        **{f"control_{key}": value for key, value in control.items()},
    }


def syn_flood_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    sample = _syn_sample(problem, params)
    if sample is None:
        return False, {"error": "target_ip_is_not_a_service"}
    ok = (
        sample["target_half_open"] < _HALF_OPEN_IDLE
        and sample["target_cookies_delta"] == 0
    )
    return ok, sample


def syn_flood(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Flood SYNs pile up half-open handshakes on the target listener only."""
    sample = _syn_sample(problem, params)
    if sample is None:
        return False, {"error": "target_ip_is_not_a_service"}
    control_ok = (
        sample["control_half_open"] < _HALF_OPEN_IDLE
        and sample["control_cookies_delta"] == 0
    )
    verified = (
        syn_queue_pressure(
            sample["target_half_open"],
            sample["target_listener_backlog"],
            sample["target_cookies_delta"],
        )
        and control_ok
    )
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={**sample, "control_ok": control_ok},
    )


# Single-datagram flows with distinct source ports; INT maps each port to its
# egress on the faulted switch before inject. Flows on the faulted port and on
# the other ports share one sending window, so transient BMv2 loss hits both.
# Pace below the real BMv2 egress queue rate so healthy queues do not drop.
_LOSS_FLOWS = 2048
_LOSS_PORT = 19407
_LOSS_LISTEN = r"""
import json, socket, sys, time
port, label, duration = int(sys.argv[1]), sys.argv[2], float(sys.argv[3])
arrived = set()
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    sock.bind(("0.0.0.0", port))
    deadline = time.monotonic() + duration
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock.settimeout(remaining)
        try:
            data, _ = sock.recvfrom(2048)
        except socket.timeout:
            break
        message = data.decode("ascii", errors="ignore")
        prefix, _, source = message.partition(":")
        if prefix == label and source.isdigit():
            arrived.add(int(source))
print(json.dumps(sorted(arrived)))
"""
_LOSS_SEND = r"""
import socket, sys, time
destinations = sys.argv[1].split(",")
port, label, count = int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
for index in range(count):
    for offset, dst in enumerate(destinations):
        source = 20000 + offset * count + index
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", source))
        sock.sendto(f"{label}:{source}".encode(), (dst, port))
        sock.close()
    time.sleep(0.025)
"""


def _loss_round(problem: Any, state: dict) -> set[int]:
    """Measure loss below the native port rate after other workload queues drain."""
    with paused_workload(
        problem.runtime,
        problem.net_env.business_workload(),
        _QUIET_SETTLE_SEC,
        hosts=endpoint_names(problem.net_env),
    ):
        return _loss_round_counted(problem, state)


def _loss_round_counted(problem: Any, state: dict) -> set[int]:
    label = f"l{time.monotonic_ns()}"
    services = state["services"]
    # Receivers finish before the round returns, including after a failed send.
    duration = _LOSS_FLOWS * 0.025 + 10
    with ThreadPoolExecutor(max_workers=len(services)) as pool:
        receivers = [
            pool.submit(
                checked_exec,
                problem.runtime.exec,
                service["name"],
                f"python3 -c {shlex.quote(_LOSS_LISTEN)} {_LOSS_PORT} {label} {duration}",
                timeout=duration + 10,
            )
            for service in services
        ]
        for service in services:
            ready = wait_until(
                lambda: bool(
                    problem.runtime.exec(
                        service["name"],
                        f"ss -Huln '( sport = :{_LOSS_PORT} )'",
                        timeout=5,
                    ).strip()
                ),
                timeout_s=5,
                interval_s=0.1,
            )
            if not ready:
                raise RuntimeError(f"Loss receiver did not bind on {service['name']}")
        checked_exec(
            problem.runtime.exec,
            state["client"],
            f"python3 -c {shlex.quote(_LOSS_SEND)} "
            f"{','.join(service['ip'] for service in services)} {_LOSS_PORT} "
            f"{label} {_LOSS_FLOWS}",
            timeout=120,
        )
        return {
            source for receiver in receivers for source in json.loads(receiver.result())
        }


def silent_loss_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Map flows to the switch egress; every flow arrives before inject."""
    model = problem.net_env.model
    switch = params.host_name
    client = model.client_on_gateway(switch) if switch in model.gateways else None
    client = client or model.clients[0]
    ports = _ecn_ports(problem, switch, params.bmv2_port)
    if ports is None or ports["sibling"] is None:
        return False, {"error": "port_has_no_downstream_sibling"}
    # A spine's egress fixes the destination leaf. Probe that leaf and a
    # sibling leaf; a gateway's uplinks share destinations through ECMP.
    services = (
        [model.services_on_leaf(ports[key].peer)[0] for key in ("probed", "sibling")]
        if ports["probed"].role == "leaf"
        else [model.services[0]]
    )
    state = {
        "client": client.name,
        "services": [{"name": service.name, "ip": service.ip} for service in services],
    }
    start_ns = _clock_ns(problem)
    arrived = _loss_round(problem, state)
    time.sleep(2)
    records = [
        record
        for service in services
        for record in _reports_since(problem, start_ns, service.ip, _LOSS_PORT)
    ]
    egress = _egress_by_source(records, model.switch_info[switch].device_id)
    state["on_port"] = sorted(p for p, out in egress.items() if out == params.bmv2_port)
    state["elsewhere"] = sorted(
        p for p, out in egress.items() if out != params.bmv2_port
    )
    problem._audit_loss = state
    ok = (
        bool(state["on_port"])
        and bool(state["elsewhere"])
        and len(arrived) >= 0.99 * _LOSS_FLOWS * len(services)
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
            "services": state["services"],
            "flows_on_port": len(on_port),
            "flows_elsewhere": len(elsewhere),
            "port_loss_percent": port_loss,
            "other_loss_percent": other_loss,
            "control_ok": bool(elsewhere) and other_loss < port_loss,
        },
    )


# The burst exceeds one egress's capacity by 2/3. Other hops split it over
# at least two ports, keeping each below the same queue rate. Reuse the
# destination port selected by the INT path probe to preserve the ECMP path.
_ECN_PPS = QUEUE_RATE_PPS * 5 // 3
_ECN_SECONDS = 4
_ECN_BASELINE_ATTEMPTS = 3
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
    # Scheduling delays must not turn the remaining datagrams into a catch-up burst.
    time.sleep(1.0 / pps)
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
            ends = {
                "client": client.name,
                "client_ip": client.ip,
                "service_ip": service.ip,
            }
            egress = _egress_by_source(
                _send(problem, ends, _SMALL_PAYLOAD, sources), device_id
            )
            flows.setdefault(client.name, []).extend(
                f"{service.ip}:{p}"
                for p, out in sorted(egress.items())
                if out == port.bmv2_port
            )
    return {client: found for client, found in flows.items() if found}


def _ecn_burst(
    problem: Any, flows: dict[str, list[str]], *, require_marking: bool = False
) -> dict[str, Any]:
    """Send the converging burst; return datagrams and CE marks at the services.

    BMv2 rate-limits real egress queues. Pause the business workload while
    the burst runs so the marks come from the probed path only.
    """
    with paused_workload(
        problem.runtime,
        problem.net_env.business_workload(),
        _QUIET_SETTLE_SEC,
        hosts=endpoint_names(problem.net_env),
    ):
        readings = []
        for _ in range(_ECN_BASELINE_ATTEMPTS if require_marking else 1):
            counts = _ecn_burst_counted(problem, flows)
            readings.append(counts)
            if counts["total"] > 0 and counts["ce"] > 0:
                break
        return {**counts, "readings": readings} if require_marking else counts


def _ecn_burst_counted(problem: Any, flows: dict[str, list[str]]) -> dict[str, int]:
    services = {flow.split(":")[0] for found in flows.values() for flow in found}
    receivers = [s.name for s in problem.net_env.model.services if s.ip in services]
    # Senders start in separate execs; a shared start time keeps their bursts
    # overlapping on the probed port.
    lead = 2.0 + len(flows)
    listen = lead + _ECN_SECONDS + 4
    path = f"/tmp/probe-ecn-{time.time_ns()}.json"
    try:
        for name in receivers:
            problem.runtime.exec(
                name,
                f"nohup python3 -c {shlex.quote(_ECN_LISTEN)} "
                f"{_INT_PORT} {listen} {path} >/dev/null 2>&1 </dev/null & echo $! > {path}.pid",
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
            out = problem.runtime.exec(name, f"cat {path} 2>/dev/null", timeout=20)
            try:
                sample = json.loads(out.strip() or "{}")
            except json.JSONDecodeError:
                sample = {}
            counts["total"] += int(sample.get("total", 0))
            counts["ce"] += int(sample.get("ce", 0))
        return counts
    finally:
        for name in receivers:
            _stop_file_probe(problem, name, path)


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
    # Keep traffic paused while the controlled path settles into marking.
    counts = _ecn_burst(problem, flows, require_marking=True)
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
