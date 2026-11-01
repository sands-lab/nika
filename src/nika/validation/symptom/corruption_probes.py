"""Symptom probes for corruption and capacity link faults (test-path only).

The baseline chooses the probe path on the healthy lab, measures the same
metrics the symptom reads, and keeps both on the problem.
"""

from __future__ import annotations

import ipaddress
import json
import re
import time
from typing import Any

from nika.net_env.verify import (
    _start_iperf3_server,
    _stop_iperf3_server,
    exec_or_empty,
    ping_stats,
)
from nika.problems.base import build_verify_result
from nika.runtime.base import LabRuntime
from nika.runtime.spec import NodeRole
from nika.validation.symptom.addressing_probes import healthy_read, lab_addresses
from nika.validation.symptom.probe import _resolve_path

# Echo train for frame loss: 300 echoes every 30 ms.
ECHO_COUNT = 300
ECHO_INTERVAL_SEC = 0.03
LOSS_MIN_PERCENT = 2.0
IPERF_SECONDS = 5
THROUGHPUT_MAX_RATIO = 0.5
# Healthy-path echo loss the baseline tolerates.
_HEALTHY_ECHO_LOSS_MAX = 0.5
CAPACITY_MAX_RATIO = 0.1
# Short timed TCP transfers over many fresh connections, so many five-tuples
# cross the device whatever subset of flows it corrupts. A timed test also ends
# when a flow stalls on a segment the device corrupts on every retransmission.
FLOW_ROUNDS = 2
FLOWS_PER_ROUND = 16
ROUND_SECONDS = 2
CSUM_ERRORS_MIN = 5


def _ends(problem: Any, params: Any) -> dict[str, Any]:
    """The scenario path across the fault and the host that owns its destination."""
    topo_size = getattr(problem.net_env, "topo_size", None) or "s"
    path = _resolve_path(problem.scenario_name, params, topo_size=topo_size)
    if path is None or not path.dst_ip:
        return {"error": "no_probe_path"}
    dst_ip = ipaddress.ip_address(path.dst_ip)
    owner = next(
        (
            node
            for node, rows in lab_addresses(problem.runtime).items()
            if any(ipaddress.ip_interface(addr).ip == dst_ip for _i, addr in rows)
        ),
        None,
    )
    if owner is None:
        return {"error": "no_node_owns_the_probe_destination", "dst": path.dst_ip}
    return {"src": path.src_host, "dst": path.dst_ip, "dst_host": owner}


def _iperf(
    runtime: LabRuntime,
    ends: dict[str, Any],
    *,
    port: int,
    args: str,
    seconds: int,
) -> dict[str, Any]:
    """Run one iperf3 test from ``src`` to ``dst_host`` and read both ends."""
    pid = _start_iperf3_server(runtime, ends["dst_host"], port)
    try:
        raw = exec_or_empty(
            runtime,
            ends["src"],
            f"timeout {seconds + 20} iperf3 -c {ends['dst']} -p {port} {args} -J",
            timeout=seconds + 30,
        )
    finally:
        _stop_iperf3_server(runtime, ends["dst_host"], pid)
    try:
        data = json.loads(raw[raw.index("{") :])
    except ValueError:
        return {"error": "no_iperf_result", "raw": raw[-300:]}
    end = data.get("end") or {}
    received = end.get("sum_received") or {}
    sent = end.get("sum_sent") or {}
    return {
        "error": data.get("error"),
        "received_bps": received.get("bits_per_second"),
        "received_bytes": received.get("bytes"),
        "retransmits": sent.get("retransmits"),
    }


def _goodput(runtime: LabRuntime, ends: dict[str, Any], port: int) -> dict[str, Any]:
    """Receiver-side TCP goodput from ``src`` toward ``dst``."""
    return _iperf(runtime, ends, port=port, args=f"-t {IPERF_SECONDS}", seconds=15)


def _link_corruption_reading(problem: Any, ends: dict[str, Any]) -> dict[str, Any]:
    runtime = problem.runtime
    echo = ping_stats(
        runtime,
        ends["src"],
        ends["dst"],
        count=ECHO_COUNT,
        interval_sec=ECHO_INTERVAL_SEC,
    )
    return {
        "echo_loss_percent": echo.loss_percent,
        "echo_received": echo.received,
        "tcp": _goodput(runtime, ends, port=15221),
    }


def link_corruption_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    ends = _ends(problem, params)
    if "error" in ends:
        return False, ends
    # A busy host can drop a few healthy echoes; re-read before judging the path.
    reading, _reads = healthy_read(
        lambda: _link_corruption_reading(problem, ends),
        lambda r: 0.0 if r["echo_loss_percent"] <= _HEALTHY_ECHO_LOSS_MAX else 1.0,
    )
    problem._audit_corruption = {"ends": ends, "before": reading}
    ok = reading["echo_loss_percent"] <= _HEALTHY_ECHO_LOSS_MAX and bool(
        reading["tcp"]["received_bps"]
    )
    return ok, {**ends, **reading}


def evaluate_link_corruption_symptom(
    problem: Any, params: Any
) -> tuple[bool, dict[str, Any]]:
    """Corrupted frames are dropped: echoes are lost and TCP goodput collapses."""
    state = getattr(problem, "_audit_corruption", None) or {}
    if not state:
        return False, {"error": "no_baseline_reading"}
    before = state["before"]
    after = _link_corruption_reading(problem, state["ends"])
    before_bps = before["tcp"]["received_bps"]
    after_bps = after["tcp"]["received_bps"]
    frame_loss = (
        after["echo_received"] > 0
        and after["echo_loss_percent"] >= LOSS_MIN_PERCENT
        and before["echo_loss_percent"] <= _HEALTHY_ECHO_LOSS_MAX
    )
    goodput_drop = bool(before_bps) and (
        after_bps is None or float(after_bps) < float(before_bps) * THROUGHPUT_MAX_RATIO
    )
    verified = frame_loss and goodput_drop
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **state["ends"],
            "before": before,
            "after": after,
            "comparison": {"frame_loss": frame_loss, "goodput_drop": goodput_drop},
        },
    )


def _tcp_csum_errors(runtime: LabRuntime, host: str) -> int | None:
    """``InCsumErrors`` from the host's ``Tcp:`` counters in /proc/net/snmp."""
    rows = [
        line.split()
        for line in exec_or_empty(runtime, host, "cat /proc/net/snmp").splitlines()
        if line.startswith("Tcp:")
    ]
    if len(rows) != 2 or "InCsumErrors" not in rows[0]:
        return None
    return int(rows[1][rows[0].index("InCsumErrors")])


def _forwarding_reading(problem: Any, ends: dict[str, Any]) -> dict[str, Any]:
    """Checksum failures the two end hosts count during fresh TCP transfers."""
    runtime = problem.runtime
    hosts = (ends["src"], ends["dst_host"])
    start = {host: _tcp_csum_errors(runtime, host) for host in hosts}
    transfers = [
        _iperf(
            runtime,
            ends,
            port=15231 + index,
            args=f"-t {ROUND_SECONDS} -P {FLOWS_PER_ROUND}{' -R' if reverse else ''}",
            seconds=ROUND_SECONDS,
        )
        for index, reverse in enumerate((False, True) * FLOW_ROUNDS)
    ]
    end = {host: _tcp_csum_errors(runtime, host) for host in hosts}
    counted = all(start[h] is not None and end[h] is not None for h in hosts)
    return {
        "tcp_csum_errors": sum(end[h] - start[h] for h in hosts) if counted else None,
        "retransmits": sum(t.get("retransmits") or 0 for t in transfers),
        "flows": len(transfers) * FLOWS_PER_ROUND,
        "transfer_errors": [t["error"] for t in transfers if t.get("error")],
    }


def _crossing_ends(problem: Any, params: Any) -> dict[str, Any]:
    """End hosts whose traffic leaves the forwarding device through ``intf_name``.

    The scenario path is kept when the device routes its destination out of
    that port, when the device is a switch without its own routes, or when its
    Linux routing table does not route the destination out of any of its
    topology ports (SR Linux routes outside the container's kernel, and SONiC
    names the port ``Ethernet<n>``), in which case the case's declared path
    stands. On a router whose port the scenario path does not
    use, the receiver is an end host the device routes out of that port, and
    the sender is an end host whose traceroute to it passes the device.
    """
    from nika.problems.rca.inventory import interfaces_for_node

    runtime = problem.runtime
    device, intf = params.forwarding_device, params.intf_name
    ends = _ends(problem, params)
    identity = problem.net_env.machine_identities.get(device)
    if getattr(identity, "role", None) is not NodeRole.ROUTER:
        return ends
    ports = set(interfaces_for_node(problem.net_env, device))

    def route_dev(ip: str) -> str | None:
        route = exec_or_empty(runtime, device, f"ip -o route get {ip}")
        match = re.search(r" dev (\S+)", route)
        return match.group(1) if match else None

    def egresses(ip: str) -> bool:
        return route_dev(ip) == intf

    if "error" not in ends:
        dev = route_dev(ends["dst"])
        if dev == intf or dev not in ports:
            return ends
    addresses = lab_addresses(runtime)
    device_ips = {
        str(ipaddress.ip_interface(address).ip) for _i, address in addresses[device]
    }
    hosts = [
        node
        for node in addresses
        if getattr(problem.net_env.machine_identities.get(node), "role", None)
        in {NodeRole.HOST, NodeRole.SERVICE}
        and addresses[node]
    ]
    receivers = [
        (node, str(ipaddress.ip_interface(addresses[node][0][1]).ip)) for node in hosts
    ]
    receivers = [(node, ip) for node, ip in receivers if egresses(ip)][:3]
    senders = list(dict.fromkeys([ends.get("src"), *hosts]))
    for receiver, ip in receivers:
        for sender in [s for s in senders if s and s != receiver][:12]:
            trace = exec_or_empty(
                runtime, sender, f"traceroute -n -q 1 -w 1 -m 8 {ip}", timeout=20
            )
            if device_ips & set(re.findall(r"\b\d+\.\d+\.\d+\.\d+\b", trace)):
                return {"src": sender, "dst": ip, "dst_host": receiver}
    return {**ends, "error": "no_end_host_path_leaves_the_device_port"}


def forwarding_corruption_baseline(
    problem: Any, params: Any
) -> tuple[bool, dict[str, Any]]:
    ends = _crossing_ends(problem, params)
    if "error" in ends:
        return False, ends
    reading = _forwarding_reading(problem, ends)
    problem._audit_forwarding = {"ends": ends, "before": reading}
    ok = reading["tcp_csum_errors"] == 0 and not reading["transfer_errors"]
    return ok, {**ends, **reading}


def evaluate_device_forwarding_corruption_symptom(
    problem: Any,
    params: Any,
) -> tuple[bool, dict[str, Any]]:
    """End hosts receive TCP segments that fail their checksum."""
    state = getattr(problem, "_audit_forwarding", None) or {}
    if not state:
        return False, {"error": "no_baseline_reading"}
    after = _forwarding_reading(problem, state["ends"])
    errors = after["tcp_csum_errors"]
    verified = (
        state["before"]["tcp_csum_errors"] == 0
        and errors is not None
        and errors >= CSUM_ERRORS_MIN
    )
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={**state["ends"], "before": state["before"], "after": after},
    )


_RX_BYTES = (
    'for i in /sys/class/net/*; do [ "${i##*/}" = lo ] || '
    "cat $i/statistics/rx_bytes; done"
)


def _rx_bytes(runtime: LabRuntime, host: str) -> int | None:
    values = exec_or_empty(runtime, host, _RX_BYTES).split()
    return (
        sum(int(v) for v in values)
        if values and all(v.isdigit() for v in values)
        else None
    )


def _delivered_rate(runtime: LabRuntime, ends: dict[str, Any]) -> dict[str, Any]:
    """Bytes per second the receiver takes in while ``src`` sends TCP for 5 s.

    The receiver's interface counters are read around the transfer, so the
    reading does not depend on iperf3 exchanging results, which a deep queue
    at a bottleneck can hold back for minutes.
    """
    pid = _start_iperf3_server(runtime, ends["dst_host"], 15201)
    try:
        start = _rx_bytes(runtime, ends["dst_host"])
        clock = time.monotonic()
        exec_or_empty(
            runtime,
            ends["src"],
            f"timeout {IPERF_SECONDS + 3} iperf3 -c {ends['dst']} -p 15201 "
            f"-t {IPERF_SECONDS} >/dev/null 2>&1",
            timeout=IPERF_SECONDS + 15,
        )
        end = _rx_bytes(runtime, ends["dst_host"])
        elapsed = time.monotonic() - clock
    finally:
        _stop_iperf3_server(runtime, ends["dst_host"], pid)
    if start is None or end is None:
        return {"error": "no_rx_counters", "received_bps": None}
    return {
        "received_bytes": end - start,
        "seconds": round(elapsed, 1),
        "received_bps": (end - start) * 8 / elapsed,
    }


def capacity_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    ends = _ends(problem, params)
    if "error" in ends:
        return False, ends
    reading = _delivered_rate(problem.runtime, ends)
    problem._audit_capacity = {"ends": ends, "before": reading}
    return bool(reading["received_bps"]), {**ends, **reading}


def evaluate_link_capacity_symptom(
    problem: Any,
    params: Any,
) -> tuple[bool, dict[str, Any]]:
    """The receiver takes in far less than on the healthy link, but traffic flows."""
    state = getattr(problem, "_audit_capacity", None) or {}
    if not state:
        return False, {"error": "no_baseline_reading"}
    before_bps = state["before"]["received_bps"]
    after = _delivered_rate(problem.runtime, state["ends"])
    after_bps = after["received_bps"]
    verified = (
        bool(before_bps)
        and bool(after_bps)
        and float(after_bps) < float(before_bps) * CAPACITY_MAX_RATIO
    )
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={**state["ends"], "before": state["before"], "after": after},
    )
