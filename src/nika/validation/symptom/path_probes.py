"""Path symptom probes for link, static-route, and MTU faults (test-path only).

The baseline chooses the probe endpoints on the healthy lab and keeps them on
the problem, so the sham, symptom, and later reads measure the same path.
"""

from __future__ import annotations

import ipaddress
import re
import time
from typing import Any

from nika.net_env.verify import exec_or_empty, srl_bgp_established_peers
from nika.problems.base import build_verify_result
from nika.problems.rca.inventory import (
    iter_link_termination_points,
    parse_endpoint,
    resolve_default_intf,
)
from nika.runtime.base import LabRuntime
from nika.runtime.spec import NodeRole
from nika.validation.symptom.addressing_probes import (
    _HOST_PREFIXES,
    address_baseline,
    address_unreachable,
    lab_addresses,
)
from nika.validation.symptom.probe import _is_endpoint_host, _resolve_path

ECHO_COUNT = 10
LINK_READS = 3
LINK_READ_GAP_SEC = 5.0
# Echo payloads: one that fits a 1500-byte MTU with room to spare, the
# largest that fits, and a small one that fits any MTU.
_LARGE_PAYLOAD = 1400
_MAX_PAYLOAD = 1472
_SMALL_PAYLOAD = 64


def echo(
    runtime: LabRuntime,
    src: str,
    dst: str,
    *,
    count: int = ECHO_COUNT,
    interval_sec: float = 0.2,
    size: int | None = None,
    df: bool = False,
) -> dict[str, Any]:
    """Send ``count`` echoes and return the replies with their TTLs."""
    size_arg = f" -s {size}" if size is not None else ""
    df_arg = " -M do" if df else ""
    output = exec_or_empty(
        runtime,
        src,
        f"ping -n -c {count} -i {interval_sec} -W 1{size_arg}{df_arg} {dst}",
        timeout=count * interval_sec + 10.0,
    )
    replies = dict(re.findall(r"icmp_seq=(\d+) ttl=(\d+)", output))
    received = len(replies)
    return {
        "src": src,
        "dst": dst,
        "sent": count,
        "received": received,
        "loss_percent": round(100.0 * (count - received) / count, 1),
        "ttls": sorted({int(ttl) for ttl in replies.values()}),
    }


def _is_end_host(problem: Any, node: str) -> bool:
    identity = problem.net_env.machine_identities.get(node)
    if identity is not None:
        return identity.role in {NodeRole.HOST, NodeRole.SERVICE}
    return _is_endpoint_host(node)


def _topo_size(problem: Any) -> str:
    return getattr(problem.net_env, "topo_size", None) or "s"


def link_path_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Echo replies across the link before inject.

    An end host's attachment is observed from a peer that pings the host's
    address. A transit link is observed on the scenario path across it.
    """
    host = params.host_name
    if _is_end_host(problem, host):
        intf = resolve_default_intf(params.intf_name, problem.net_env)
        problem._audit_link_path = {"mode": "end_host"}
        ok, evidence = address_baseline(problem, host, intf)
        return ok, {"mode": "end_host", **evidence}
    path = _resolve_path(problem.scenario_name, params, topo_size=_topo_size(problem))
    if path is None or not path.dst_ip:
        return False, {"error": "no_probe_path"}
    before = echo(problem.runtime, path.src_host, path.dst_ip)
    problem._audit_link_path = {"mode": "transit", "before": before}
    return before["loss_percent"] == 0.0, {"mode": "transit", **before}


def link_path_lost(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Traffic no longer crosses the link.

    On a transit link the path is either cut (every echo lost) or the routing
    protocol moves it to a longer detour, so every reply arrives with a lower
    TTL than any reply on the healthy path.
    """
    state = getattr(problem, "_audit_link_path", None) or {}
    if state.get("mode") == "end_host":
        return address_unreachable(problem, "attached_host")
    before = state.get("before")
    if not before:
        return False, {"error": "no_baseline_path"}
    # The routing protocol may still be converging right after inject; a
    # partial read is retried until the path settles.
    reads: list[dict[str, Any]] = []
    for attempt in range(LINK_READS):
        if attempt:
            time.sleep(LINK_READ_GAP_SEC)
        after = echo(problem.runtime, before["src"], before["dst"])
        reads.append(after)
        cut = after["received"] == 0
        rerouted = (
            after["received"] == after["sent"]
            and bool(before["ttls"])
            and max(after["ttls"]) < min(before["ttls"])
        )
        if cut or rerouted or after["ttls"] == before["ttls"]:
            break
    verified = cut or rerouted
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "mode": "transit",
            "before": before,
            "after": after,
            "reads": len(reads),
            "path_cut": cut,
            "rerouted": rerouted,
        },
    )


def largest_reply_packet(runtime: LabRuntime, src: str, dst: str) -> int | None:
    """Largest IP packet in which replies to large echoes reach ``src``.

    An undersized hop on the return path fragments the replies (echo replies
    do not set DF), so ``src`` receives pieces no larger than that hop's MTU.
    """
    output = exec_or_empty(
        runtime,
        src,
        f"timeout 6 tcpdump -nv -l -c 40 -i any 'icmp and src host {dst}' "
        "2>/dev/null & sleep 1; "
        f"ping -n -c 3 -i 0.3 -W 1 -s {_LARGE_PAYLOAD} {dst} >/dev/null; wait",
        timeout=20.0,
    )
    lengths = [int(n) for n in re.findall(r"proto ICMP \(1\), length (\d+)", output)]
    return max(lengths) if lengths else None


def _df_passes(runtime: LabRuntime, src: str, dst: str, size: int) -> bool:
    reply = echo(runtime, src, dst, count=2, interval_sec=0.2, size=size, df=True)
    return reply["received"] > 0


def largest_df_packet(runtime: LabRuntime, src: str, dst: str) -> int | None:
    """Largest don't-fragment IP packet from ``src`` that gets a reply (8-byte steps)."""
    low, high = _SMALL_PAYLOAD, _MAX_PAYLOAD
    if not _df_passes(runtime, src, dst, low):
        return None
    if _df_passes(runtime, src, dst, high):
        return high + 28
    while high - low > 8:
        middle = (low + high) // 2
        if _df_passes(runtime, src, dst, middle):
            low = middle
        else:
            high = middle
    return low + 28


def _mtu_ends(problem: Any, params: Any) -> dict[str, str] | None:
    """Endpoints whose traffic must cross the shrunk interface, and the probe.

    On a point-to-point link, a host on the scenario path sends don't-fragment
    echoes to the far end of the link: a connected route that only this link
    reaches, so a meshed lab cannot route around the low-MTU hop. On a shared
    segment, an end host on that segment pings the node's address there, and
    the replies leave through the shrunk interface.
    """
    runtime = problem.runtime
    host, intf = params.host_name, params.intf_name
    peer = problem._peer_endpoint(host, intf)
    if peer is not None:
        path = _resolve_path(
            problem.scenario_name, params, topo_size=_topo_size(problem)
        )
        address = runtime.get_host_ip(peer[0], peer[1], with_prefix=True)
        if path is None or not address:
            return None
        return {
            "src": path.src_host,
            "dst": address.split("/")[0],
            "probe": "forward_df",
        }
    endpoint = f"{host}:{intf}"
    members = next(
        (
            [str(ep) for ep in tps]
            for _key, tps in iter_link_termination_points(problem.net_env)
            if endpoint in map(str, tps)
        ),
        [],
    )
    src = next(
        (
            node
            for node, _intf in map(parse_endpoint, members)
            if node and node != host and _is_end_host(problem, node)
        ),
        None,
    )
    address = runtime.get_host_ip(host, intf, with_prefix=True)
    if src is None or not address:
        return None
    return {"src": src, "dst": address.split("/")[0], "probe": "reply_size"}


def _largest_packet(runtime: LabRuntime, ends: dict[str, str]) -> int | None:
    measure = (
        largest_df_packet if ends["probe"] == "forward_df" else largest_reply_packet
    )
    return measure(runtime, ends["src"], ends["dst"])


def mtu_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    ends = _mtu_ends(problem, params)
    if ends is None:
        return False, {"error": "no_probe_path"}
    largest = _largest_packet(problem.runtime, ends)
    problem._audit_mtu = {**ends, "before_bytes": largest}
    return (largest or 0) >= _LARGE_PAYLOAD + 28, {**ends, "largest_bytes": largest}


def mtu_reduced(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Small packets still cross, but the largest packet that does is far smaller."""
    state = getattr(problem, "_audit_mtu", None) or {}
    before = state.get("before_bytes")
    if not before:
        return False, {"error": "no_baseline_packet_size"}
    after = _largest_packet(problem.runtime, state)
    verified = after is not None and after <= before // 2
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={**state, "after_bytes": after},
    )


def _blackhole_ends(problem: Any, params: Any) -> dict[str, Any]:
    """An attached host, a live address in the prefix inject drops, and a control.

    The control address sits outside that prefix and is reached through the
    same router, so it stays reachable when only the prefix is blackholed.
    """
    runtime = problem.runtime
    router = params.host_name
    network = ipaddress.ip_network(problem.blackhole_network(params), strict=False)
    sources = problem.attached_hosts(params)
    inside: list[tuple[str, str]] = []
    outside: list[tuple[str, str]] = []
    addresses = lab_addresses(runtime)
    for node, rows in addresses.items():
        if node == router:
            continue
        for _intf, address in rows:
            ip = ipaddress.ip_interface(address).ip
            (inside if ip in network else outside).append((node, str(ip)))

    def first_reachable(src: str, candidates: list[tuple[str, str]]) -> str | None:
        ordered = sorted(candidates, key=lambda c: not c[0].startswith(_HOST_PREFIXES))
        for node, ip in ordered[:12]:
            if node != src and echo(runtime, src, ip, count=3)["received"] == 3:
                return ip
        return None

    # Hosts on another VRF or VLAN of the router may have no route to the prefix.
    for src in sources[:6]:
        dst = first_reachable(src, inside)
        if dst:
            # The control is routed by the router too: off the source's subnet.
            local = [
                ipaddress.ip_interface(address).network
                for _intf, address in addresses.get(src, [])
            ]
            remote = [
                (node, ip)
                for node, ip in outside
                if not any(ipaddress.ip_address(ip) in net for net in local)
            ]
            control = first_reachable(src, remote)
            if control is None and problem.lab_backend == "containerlab":
                # SR Linux data-plane addresses do not appear in Linux `ip addr`.
                # An established BGP peer is a live router beyond the injected
                # router and provides a control path outside the dropped prefix.
                peers = srl_bgp_established_peers(
                    exec_or_empty(
                        runtime,
                        router,
                        'sr_cli "show network-instance default protocols bgp neighbor"',
                    )
                )
                control = first_reachable(
                    src,
                    [
                        (router, ip)
                        for ip in sorted(peers)
                        if ipaddress.ip_address(ip) not in network
                        and not any(ipaddress.ip_address(ip) in net for net in local)
                    ],
                )
            return {
                "router": router,
                "network": str(network),
                "src": src,
                "dst": dst,
                "control_dst": control,
            }
    return {"router": router, "network": str(network), "sources": sources[:6]}


def blackhole_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    ends = _blackhole_ends(problem, params)
    problem._audit_blackhole = ends
    if not ends.get("dst") or not ends.get("control_dst"):
        return False, {**ends, "error": ends.get("error") or "no_live_destination"}
    target = echo(problem.runtime, ends["src"], ends["dst"])
    control = echo(problem.runtime, ends["src"], ends["control_dst"])
    ok = target["loss_percent"] == 0.0 and control["loss_percent"] == 0.0
    return ok, {**ends, "target": target, "control": control}


def blackhole_dropped(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Hosts behind the router lose the prefix; other destinations stay up."""
    ends = getattr(problem, "_audit_blackhole", None) or {}
    if not ends.get("dst") or not ends.get("control_dst"):
        return False, {"error": "no_baseline_path"}
    target = echo(problem.runtime, ends["src"], ends["dst"])
    control = echo(problem.runtime, ends["src"], ends["control_dst"])
    control_ok = control["loss_percent"] == 0.0
    verified = target["received"] == 0 and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **ends,
            "target": target,
            "control": control,
            "control_ok": control_ok,
        },
    )
