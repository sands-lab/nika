"""Duplicate-address symptom probes (test-path only).

A third host pings the affected address before and after inject. A ping to a
different host from the same observer is the control path.
"""

from __future__ import annotations

from typing import Any

from nika.net_env.verify import ping_stats
from nika.problems.base import build_verify_result
from nika.runtime.base import LabRuntime

_HOST_PREFIXES = ("client", "pc", "web", "service", "host", "server")


def _eth0_ip(runtime: LabRuntime, node: str) -> str:
    out = runtime.exec(
        node, "ip -4 -o addr show dev eth0 scope global | awk '{print $4}'"
    )
    return out.strip().split("/")[0]


def _reachable(runtime: LabRuntime, src: str, ip: str) -> bool:
    return ping_stats(runtime, src, ip, count=3, interval_sec=0.2).loss_percent == 0.0


def _observers(runtime: LabRuntime, target_ip: str, exclude: set[str]) -> dict:
    """Pick a host that reaches ``target_ip`` and one more host as control.

    Labs whose only hosts are the faulted pair (llmd_lab) observe from any
    other node on the segment.
    """
    nodes = [node for node in sorted(runtime.list_nodes()) if node not in exclude]
    candidates = [node for node in nodes if node.startswith(_HOST_PREFIXES)] or nodes
    observer = next(
        (node for node in candidates[:12] if _reachable(runtime, node, target_ip)),
        None,
    )
    if observer is None:
        return {}
    for node in candidates:
        if node == observer:
            continue
        ip = _eth0_ip(runtime, node)
        if ip and ip != target_ip and _reachable(runtime, observer, ip):
            return {"observer": observer, "control_ip": ip, "target_ip": target_ip}
    return {}


def _loss(runtime: LabRuntime, src: str, ip: str) -> float:
    return ping_stats(runtime, src, ip, count=10, interval_sec=0.2).loss_percent


def _baseline(problem: Any, target_host: str, exclude: set[str]) -> tuple[bool, dict]:
    target_ip = _eth0_ip(problem.runtime, target_host)
    ends = _observers(problem.runtime, target_ip, exclude)
    problem._audit_address_ends = ends
    return bool(ends), {"target_host": target_host, **ends}


def _symptom(problem: Any, label: str) -> tuple[bool, dict[str, Any]]:
    ends = getattr(problem, "_audit_address_ends", None) or {}
    if not ends:
        return False, {"error": "no_baseline_observer"}
    runtime = problem.runtime
    target_loss = _loss(runtime, ends["observer"], ends["target_ip"])
    control_loss = _loss(runtime, ends["observer"], ends["control_ip"])
    control_ok = control_loss == 0.0
    verified = target_loss >= 50.0 and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **ends,
            "target": label,
            "target_loss_percent": target_loss,
            "control_loss_percent": control_loss,
            "control_ok": control_ok,
        },
    )


def mac_conflict_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    return _baseline(problem, params.host_name, {params.host_name, params.host_name_2})


def mac_conflict(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Frames for the host whose MAC changed no longer reach it."""
    return _symptom(problem, "host_with_copied_mac")


def ip_conflict_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    return _baseline(
        problem, params.host_name_2, {params.host_name, params.host_name_2}
    )


def ip_conflict(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The host that took a duplicate address loses its own address."""
    return _symptom(problem, "original_address_of_reconfigured_host")
