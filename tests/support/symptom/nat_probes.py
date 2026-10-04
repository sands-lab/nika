"""SNAT symptom probes for edge NAT failures (test-path only).

Each probe runs a TCP echo listener on the edge's WAN next hop and drives
connections from an inside host in ``source_prefix``. The listener logs the
translated source address of every accepted connection.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shlex
import time
from typing import Any

from nika.problems.base import build_verify_result
from nika.runtime.base import LabRuntime

_PORT = 18080
_SERVER_LOG = "/tmp/nika-audit-nat-peers.log"
_FLOW_STATUS = "/tmp/nika-audit-nat-flow.status"

_SERVER = rf"""
import socket, threading
log = open({_SERVER_LOG!r}, "a", buffering=1)
def serve(conn, peer):
    log.write(peer[0] + "\n")
    try:
        while True:
            data = conn.recv(1024)
            if not data:
                break
            conn.sendall(data)
    except OSError:
        pass
srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", {_PORT}))
srv.listen(4096)
while True:
    conn, peer = srv.accept()
    threading.Thread(target=serve, args=(conn, peer), daemon=True).start()
"""

# Opens ``count`` concurrent connections and holds them until all attempts end.
_BURST = r"""
import json, socket, sys, threading
target, port, count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
held, failed, lock = [], [0], threading.Lock()
def attempt():
    sock = socket.socket()
    sock.settimeout(2)
    try:
        sock.connect((target, port))
        sock.sendall(b"x")
        sock.recv(1)
        with lock:
            held.append(sock)
    except OSError:
        with lock:
            failed[0] += 1
threads = [threading.Thread(target=attempt) for _ in range(count)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
print(json.dumps({"connected": len(held), "failed": failed[0]}))
"""

# One long-lived flow that exchanges a byte every 0.5 s.
_FLOW = rf"""
import socket, sys, time
def status(text):
    open({_FLOW_STATUS!r}, "w").write(text)
try:
    sock = socket.create_connection((sys.argv[1], {_PORT}), timeout=3)
    sock.settimeout(3)
    deadline = time.time() + 1800
    while time.time() < deadline:
        sock.sendall(b"k")
        if sock.recv(1) != b"k":
            raise OSError("closed")
        status("alive")
        time.sleep(0.5)
except OSError:
    status("broken")
"""


def _addresses(
    runtime: LabRuntime, node: str
) -> list[tuple[str, ipaddress.IPv4Interface]]:
    out = runtime.exec(node, "ip -4 -o addr show", timeout=10)
    found = []
    for line in out.splitlines():
        match = re.match(r"\d+:\s+(\S+)\s+inet\s+(\S+)", line)
        if match:
            found.append((match.group(1), ipaddress.IPv4Interface(match.group(2))))
    return found


def _endpoints(runtime: LabRuntime, edge: str, source_prefix: str) -> dict[str, str]:
    """Find an inside host in ``source_prefix`` and the edge's WAN next hop."""
    prefix = ipaddress.IPv4Network(source_prefix)
    inside_intf = next(
        (intf for intf, addr in _addresses(runtime, edge) if addr.ip in prefix), None
    )
    if inside_intf is None:
        raise RuntimeError(f"{edge} has no interface in {source_prefix}")
    master = re.search(
        r"\bmaster (\S+)", runtime.exec(edge, f"ip -o link show dev {inside_intf}")
    )
    table = f"vrf {master.group(1)}" if master else ""
    route = runtime.exec(edge, f"ip -4 route show {table} default", timeout=10)
    via = re.search(r"\bvia (\S+)", route)
    if via is None:
        raise RuntimeError(f"{edge} has no default route for {source_prefix}")
    inside = server = None
    for node in runtime.get_connected_devices(edge):
        addrs = _addresses(runtime, node)
        if inside is None and any(addr.ip in prefix for _, addr in addrs):
            inside = node
        if any(str(addr.ip) == via.group(1) for _, addr in addrs):
            server = node
    if inside is None or server is None:
        raise RuntimeError(
            f"no inside host or WAN peer next to {edge}: inside={inside} server={server}"
        )
    return {"inside": inside, "server": server, "server_ip": via.group(1)}


def _start_server(runtime: LabRuntime, server: str) -> None:
    running = runtime.exec(server, f"ss -ltn 'sport = :{_PORT}' | tail -n +2")
    if running.strip():
        return
    runtime.exec(
        server,
        f"nohup python3 -u -c {shlex.quote(_SERVER)} >/dev/null 2>&1 </dev/null &",
    )
    time.sleep(0.5)


def _peer_count(runtime: LabRuntime, server: str, address: str) -> int:
    out = runtime.exec(server, f"grep -cx {shlex.quote(address)} {_SERVER_LOG} || true")
    return int((out.strip().splitlines() or ["0"])[-1])


def _burst(runtime: LabRuntime, ends: dict[str, str], count: int) -> dict[str, int]:
    out = runtime.exec(
        ends["inside"],
        f"python3 -c {shlex.quote(_BURST)} {ends['server_ip']} {_PORT} {count}",
        timeout=60,
    )
    return json.loads(out.strip().splitlines()[-1])


def _snat_pool_size(params: Any) -> int:
    return int(params.port_end) - int(params.port_start) + 1


def snat_pool_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Before inject, more concurrent flows than the pool size all connect."""
    ends = _endpoints(problem.runtime, params.host_name, params.source_prefix)
    problem._audit_nat_ends = ends
    _start_server(problem.runtime, ends["server"])
    count = _snat_pool_size(params) + 16
    burst = _burst(problem.runtime, ends, count)
    ok = burst["connected"] == count
    return ok, {"endpoints": ends, "attempted": count, **burst}


def snat_pool_exhaustion(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """After inject, concurrent flows stop at the SNAT pool size."""
    ends = getattr(problem, "_audit_nat_ends", None) or _endpoints(
        problem.runtime, params.host_name, params.source_prefix
    )
    _start_server(problem.runtime, ends["server"])
    pool = _snat_pool_size(params)
    count = pool + 16
    burst = _burst(problem.runtime, ends, count)
    verified = 0 < burst["connected"] <= pool and burst["failed"] > 0
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={"endpoints": ends, "attempted": count, "pool_size": pool, **burst},
    )


def nat_flow_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Start one long-lived flow through ``nat_ip_a`` before the mapping moves."""
    runtime = problem.runtime
    ends = _endpoints(runtime, params.host_name, params.source_prefix)
    problem._audit_nat_ends = ends
    _start_server(runtime, ends["server"])
    before = _peer_count(runtime, ends["server"], params.nat_ip_a)
    runtime.exec(ends["inside"], f"rm -f {_FLOW_STATUS}")
    runtime.exec(
        ends["inside"],
        f"nohup python3 -c {shlex.quote(_FLOW)} {ends['server_ip']} "
        ">/dev/null 2>&1 </dev/null &",
    )
    status = ""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and status != "alive":
        time.sleep(0.5)
        status = runtime.exec(ends["inside"], f"cat {_FLOW_STATUS} 2>/dev/null").strip()
    via_a = _peer_count(runtime, ends["server"], params.nat_ip_a) > before
    return status == "alive" and via_a, {
        "endpoints": ends,
        "flow_status": status,
        "flow_translated_to_nat_ip_a": via_a,
    }


def nat_mapping_removed(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The pre-inject flow breaks and new flows leave through ``nat_ip_b``."""
    runtime = problem.runtime
    ends = getattr(problem, "_audit_nat_ends", None) or _endpoints(
        runtime, params.host_name, params.source_prefix
    )
    status = ""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = runtime.exec(ends["inside"], f"cat {_FLOW_STATUS} 2>/dev/null").strip()
        if status == "broken":
            break
        time.sleep(0.5)
    before_b = _peer_count(runtime, ends["server"], params.nat_ip_b)
    fresh = _burst(runtime, ends, 1)
    via_b = _peer_count(runtime, ends["server"], params.nat_ip_b) > before_b
    verified = status == "broken" and fresh["connected"] == 1 and via_b
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "endpoints": ends,
            "old_flow_status": status,
            "new_flow_connected": fresh["connected"] == 1,
            "new_flow_translated_to_nat_ip_b": via_b,
        },
    )
