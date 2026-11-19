"""NAT symptom checks against the declared public HTTP workload.

Healthy baselines read existing long-lived sessions or make a bounded
concurrency request. They leave no listener, client process or probe files
in a case image.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import re
import shlex
import time
from typing import Any

from nika.problems.base import build_verify_result
from nika.runtime.base import LabRuntime, RuntimeCapabilityError
from nika.traffic.workload import NAT_SESSIONS

from nika.problems.support.nat_sessions import TCP_BURST as _BURST


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


def _endpoints(problem: Any, edge: str, source_prefix: str) -> dict[str, str]:
    """Find an inside host in ``source_prefix`` and an outside server.

    Addresses are read on the hosts, so the edge may run any router platform.
    """
    runtime = problem.runtime
    workload = problem.net_env.business_workload()
    if workload is None or not workload.covers(NAT_SESSIONS, (edge, source_prefix)):
        raise RuntimeCapabilityError(
            "NAT validation requires declared NAT session traffic"
        )
    prefix = ipaddress.IPv4Network(source_prefix)
    declared = problem.net_env.nat_probe_server()
    if declared is not None:
        inside = next(
            (
                node
                for node in runtime.get_connected_devices(edge)
                if any(addr.ip in prefix for _, addr in _host_addresses(runtime, node))
            ),
            None,
        )
        if inside is None:
            raise RuntimeError(f"no inside host in {source_prefix} next to {edge}")
        server, server_ip = declared
        return {
            "inside": inside,
            "server": server,
            "server_ip": server_ip,
            "port": str(_service_port(problem, server)),
        }
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
    return {
        "inside": inside,
        "server": server,
        "server_ip": via.group(1),
        "port": str(_service_port(problem, server)),
    }


def _host_addresses(
    runtime: LabRuntime, node: str
) -> list[tuple[str, ipaddress.IPv4Interface]]:
    """Addresses of ``node``, or none when it is not a Linux host."""
    try:
        return _addresses(runtime, node)
    except Exception:  # noqa: BLE001 - native routers have no Linux addresses
        return []


def _service_port(problem: Any, server: str) -> int:
    workload = problem.net_env.business_workload()
    for process in workload.processes if workload else ():
        if process.node == server and process.app == "web_service":
            args = list(process.args)
            return int(args[args.index("--port") + 1]) if "--port" in args else 80
    raise RuntimeCapabilityError("NAT validation requires a declared HTTP service")


def _connections(
    runtime: LabRuntime, ends: dict[str, str], address: str
) -> dict[str, int]:
    output = runtime.exec(
        ends["server"],
        f"ss -Htn state established '( sport = :{ends['port']} and dst {address} )'",
        timeout=10,
    )
    peers = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        try:
            peer = fields[-1]
            peer_address = ipaddress.ip_address(peer.rsplit(":", 1)[0].strip("[]"))
            if isinstance(peer_address, ipaddress.IPv6Address):
                peer_address = peer_address.ipv4_mapped or peer_address
            if str(peer_address) != address:
                raise ValueError("unexpected peer")
            peers[peer] = int(fields[-3])
        except (ValueError, IndexError) as error:
            raise ValueError("Unrecognized public-service TCP socket state") from error
    return peers


def _burst(runtime: LabRuntime, ends: dict[str, str], count: int) -> dict[str, int]:
    out = runtime.exec(
        ends["inside"],
        f"python3 -c {shlex.quote(_BURST)} {ends['server_ip']} {ends['port']} {count}",
        timeout=60,
    )
    return json.loads(out.strip().splitlines()[-1])


def _snat_pool_size(params: Any) -> int:
    return int(params.port_end) - int(params.port_start) + 1


def snat_pool_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Before inject, more concurrent flows than the pool size all connect."""
    ends = _endpoints(problem, params.host_name, params.source_prefix)
    problem._audit_nat_ends = ends
    count = _snat_pool_size(params) + 16
    burst = _burst(problem.runtime, ends, count)
    ok = burst["connected"] == count
    return ok, {"endpoints": ends, "attempted": count, **burst}


def snat_pool_exhaustion(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """After inject, concurrent flows stop at the SNAT pool size."""
    ends = getattr(problem, "_audit_nat_ends", None) or _endpoints(
        problem, params.host_name, params.source_prefix
    )
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
    """Read the workload's established sessions through the original address."""
    ends = _endpoints(problem, params.host_name, params.source_prefix)
    problem._audit_nat_ends = ends
    deadline = time.monotonic() + 10
    while True:
        peers = _connections(problem.runtime, ends, params.nat_ip_a)
        if peers or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    problem._audit_nat_old_peers = sorted(peers)
    return bool(peers), {
        "endpoints": ends,
        "established_sessions": len(peers),
        "flow_translated_to_nat_ip_a": bool(peers),
    }


def nat_mapping_removed(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Old workload sessions stall while a fresh HTTP request uses the new address."""
    # A restored mapping must not be judged from sockets left by the incident.
    if not problem.verify_fault(params).get("verified"):
        return False, build_verify_result(
            fault_type=problem.root_cause_name,
            verified=False,
            details={"artifact_present": False},
        )
    runtime = problem.runtime
    ends = getattr(problem, "_audit_nat_ends", None) or _endpoints(
        problem, params.host_name, params.source_prefix
    )
    old_peers = set(getattr(problem, "_audit_nat_old_peers", ()))
    if not old_peers:
        return False, build_verify_result(
            fault_type=problem.root_cause_name,
            verified=False,
            details={"error": "No healthy workload sessions were recorded"},
        )
    before_b = set(_connections(runtime, ends, params.nat_ip_b))
    fresh_peers, blocked_peers = set(), set()
    pending_since = {}
    url = f"http://{ends['server_ip']}:{ends['port']}/events"
    command = (
        f"curl -f -s -m 35 -o /dev/null {shlex.quote(url)}; "
        "printf 'request_exit=%s\\n' \"$?\""
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(runtime.exec, ends["inside"], command, timeout=40)
        while not request.done():
            now = time.monotonic()
            pending = {
                peer
                for peer, queued in _connections(runtime, ends, params.nat_ip_a).items()
                if peer in old_peers and queued > 0
            }
            pending_since = {peer: pending_since.get(peer, now) for peer in pending}
            blocked_peers.update(
                peer for peer, started in pending_since.items() if now - started >= 1
            )
            fresh_peers.update(
                set(_connections(runtime, ends, params.nat_ip_b)) - before_b
            )
            time.sleep(0.5)
        output = request.result()
    connected = bool(re.search(r"request_exit=0\b", output))
    verified = bool(blocked_peers) and bool(fresh_peers) and connected
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "endpoints": ends,
            "stalled_old_sessions": sorted(blocked_peers),
            "new_sessions_from_nat_ip_b": sorted(fresh_peers),
            "new_flow_connected": connected,
        },
    )
