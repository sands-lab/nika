"""One BGP peer loses the target's advertised paths after TCP MD5 disagrees."""

from __future__ import annotations

import json
import time
from typing import Any

from nika.problems.base import build_verify_result
from nika.problems.support.polling import wait_until
from nika.problems.support.router_ops import router_fault_ops
from nika.validation.symptom.routing_probes import (
    bgp_baseline,
    host_addresses,
    _read_view,
    _is_up,
)


def tcp_md5_counters(netstat: str) -> dict[str, int]:
    """Read TCP MD5 receive errors from the kernel's paired netstat rows."""
    lines = netstat.splitlines()
    for header, values in zip(lines, lines[1:]):
        keys, counts = header.split(), values.split()
        if keys[:1] == ["TcpExt:"] and counts[:1] == ["TcpExt:"]:
            fields = dict(zip(keys[1:], counts[1:]))
            return {
                key: int(fields[key])
                for key in ("TCPMD5NotFound", "TCPMD5Unexpected", "TCPMD5Failure")
                if fields.get(key, "").isdigit()
            }
    return {}


def _kernel_errors(problem: Any, host: str) -> dict[str, int]:
    if router_fault_ops(problem, host) is not None:
        return {}
    command = (
        "ip netns exec srbase-default cat /proc/net/netstat"
        if problem.runtime.backend == "containerlab"
        else "cat /proc/net/netstat"
    )
    counters = tcp_md5_counters(problem.runtime.exec(host, command))
    if not counters:
        raise ValueError(f"Unreadable TCP MD5 counters on {host!r}")
    return counters


def _received_prefixes(problem: Any, router: str, peer: str) -> set[str]:
    if problem.runtime.backend == "containerlab":
        return problem.runtime.lab_api.srl_bgp_received_prefixes(router, peer)
    ops = router_fault_ops(problem, router)
    if ops is not None:
        return ops.bgp_received_prefixes(peer)
    data = json.loads(
        problem.runtime.exec(
            router, f"vtysh -c 'show bgp ipv4 unicast neighbors {peer} routes json'"
        )
    )
    if not isinstance(data.get("routes"), dict):
        raise ValueError(f"Unreadable BGP routes on {router!r}: {data}")
    return set(data["routes"])


def md5_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Record an established remote peer and the target's routes learned by it."""
    peer = problem.select_neighbor(params)
    ok, details = bgp_baseline(problem, params)
    if not ok:
        return False, details
    plan = problem._audit_routing_plan
    for key in plan["up"]:
        _, observer, local_address = key.split("|")
        ops = router_fault_ops(problem, observer)
        addresses = (
            ops.ipv4_addresses() if ops else host_addresses(problem.runtime, observer)
        )
        if peer not in addresses:
            continue
        prefixes: set[str] = set()

        def advertised():
            nonlocal prefixes
            prefixes = _received_prefixes(problem, observer, local_address)
            return bool(prefixes)

        wait_until(advertised, 60, interval_s=3)
        problem._audit_md5 = {
            "key": key,
            "observer": observer,
            "local_address": local_address,
            "prefixes": sorted(prefixes),
            "kernel_md5_before": _kernel_errors(problem, params.host_name),
        }
        return bool(prefixes), {**problem._audit_md5, "neighbor_ip": peer}
    return False, {"error": "no_remote_peer", "neighbor_ip": peer, **details}


def md5_paths_withdrawn(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The selected remote session goes down and withdraws the target's paths."""
    base = getattr(problem, "_audit_md5", None)
    if not base:
        return False, {"error": "no_healthy_md5_baseline"}
    reading: dict[str, Any] = {}
    started = time.monotonic()

    def withdrawn():
        states, _, errors = _read_view(
            problem, params.host_name, problem._audit_routing_plan
        )
        prefixes = _received_prefixes(problem, base["observer"], base["local_address"])
        lost = sorted(set(base["prefixes"]) - prefixes)
        counters = _kernel_errors(problem, params.host_name)
        before = base["kernel_md5_before"]
        # A local reset alone also withdraws routes. Require a rejected TCP
        # segment on FRR, or a full retry interval on native routers.
        authentication_observed = (
            any(counters.get(key, 0) > value for key, value in before.items())
            if before
            else time.monotonic() - started >= 90
        )
        reading.update(
            kernel_md5_after=counters,
            authentication_observed=authentication_observed,
            state=states.get(base["key"], "absent"),
            lost_prefixes=lost,
            remaining_prefixes=sorted(prefixes),
            errors=errors,
        )
        return (
            bool(lost)
            and not _is_up("bgp", states.get(base["key"]))
            and not errors
            and authentication_observed
        )

    ok = wait_until(withdrawn, 240, interval_s=3)
    return ok, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=ok,
        details={
            "host": params.host_name,
            "neighbor_ip": problem.neighbor_ip,
            "baseline": base,
            **reading,
        },
    )
