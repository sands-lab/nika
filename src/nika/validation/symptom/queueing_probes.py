"""Symptom probes for egress-queue faults (test-path only)."""

from __future__ import annotations

import json
import re
import shlex
from typing import Any

from nika.net_env.verify import exec_or_empty, ping_stats
from nika.problems.base import build_verify_result
from nika.problems.rca.inventory import iter_link_termination_points, parse_endpoint

_SAMPLE_PINGS = 30
_PING_INTERVAL_S = 0.2
# A shallow egress queue delays and may drop packets on the victim path. The
# victim must be this many times slower than its healthy reading. Relative
# delay or measured egress drops then tie the change to the affected port.
_RTT_FACTOR = 3.0
_QUEUE_DELAY_SHARE = 0.5
_RATE_RE = re.compile(r"^(\d+(?:\.\d+)?)([kmg]?)bit$", re.IGNORECASE)


def _queue_delay_ms(problem: Any, params: Any, device: str) -> float | None:
    """Drain time of the full egress queue the fault configures, from its params."""
    from nika.problems.traffic_queueing_resource.link import (
        _BMV2_RATES,
        _KERNEL_RATES,
        tc_size_bytes,
    )

    bmv2 = device in (problem.net_env.bmv2_switches or [])
    rate = params.port_rate or (_BMV2_RATES if bmv2 else _KERNEL_RATES)[0]
    match = _RATE_RE.match(rate.strip())
    limit = tc_size_bytes(params.queue_limit)
    if match is None or not limit:
        return None
    scale = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[match.group(2).lower()]
    return limit * 8 / (float(match.group(1)) * scale) * 1000.0


def _control(problem: Any, params: Any, device: str) -> tuple[str, str] | None:
    """A server or host the observer reaches without leaving ``device`` toward it."""
    links = [
        {parse_endpoint(str(ep))[0] for ep in tps}
        for _key, tps in iter_link_termination_points(problem.net_env)
    ]
    attached = set().union(*(nodes for nodes in links if device in nodes))
    observer_side = set().union(
        *(nodes for nodes in links if params.observer_device in nodes)
    )
    switches = observer_side - {params.observer_device}
    near = set().union(*(nodes for nodes in links if nodes & switches))
    services = getattr(problem.net_env, "servers", None) or {}
    servers = [*(services.get("web") or []), *(services.get("dns") or [])]
    hosts = getattr(problem.net_env, "hosts", None) or []
    skip = {params.host_name, params.observer_device, device}
    # Prefer a destination off the observer's own switch so the control crosses
    # the fabric like the victim does.
    candidates = sorted(dict.fromkeys([*servers, *hosts]), key=lambda n: n in near)
    for name in candidates:
        if name in skip or name in attached:
            continue
        try:
            ip = problem.runtime.get_host_ip(name, with_prefix=False)
        except Exception:  # noqa: BLE001 - try the next candidate
            continue
        if ip:
            return name, ip
    return None


def _receiver_ip(problem: Any, params: Any) -> str | None:
    if params.probe_dst_ip:
        return params.probe_dst_ip
    try:
        return problem.runtime.get_host_ip(params.host_name, with_prefix=False)
    except Exception:  # noqa: BLE001
        return None


def _flow(problem: Any, observer: str, dst_ip: str) -> Any:
    return ping_stats(
        problem.runtime,
        observer,
        dst_ip,
        count=_SAMPLE_PINGS,
        interval_sec=_PING_INTERVAL_S,
    )


def _sample(problem: Any, params: Any) -> dict[str, Any]:
    """RTT and loss of the victim flow and of a control flow that avoids the queue."""
    device, intf = problem._egress_port(params)
    observer = params.observer_device
    receiver_ip = _receiver_ip(problem, params)
    victim = _flow(problem, observer, receiver_ip) if observer and receiver_ip else None
    control = _control(problem, params, device)
    control_stats = (
        _flow(problem, observer, control[1]) if observer and control else None
    )
    return {
        "egress": f"{device}:{intf}",
        "queue_delay_ms": _queue_delay_ms(problem, params, device),
        "observer": observer,
        "receiver_ip": receiver_ip,
        "victim_rtt_ms": getattr(victim, "rtt_avg_ms", None),
        "victim_loss_percent": getattr(victim, "loss_percent", None),
        "control": control[0] if control else None,
        "control_rtt_ms": getattr(control_stats, "rtt_avg_ms", None),
        "control_loss_percent": getattr(control_stats, "loss_percent", None),
    }


def _delayed(sample: dict[str, Any]) -> bool:
    victim, control = sample["victim_rtt_ms"], sample["control_rtt_ms"]
    queue = sample["queue_delay_ms"]
    if victim is None or control is None or not queue:
        return False
    return (
        victim >= control * _RTT_FACTOR
        and victim - control >= queue * _QUEUE_DELAY_SHARE
    )


def incast_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Before the bursts both flows answer and the victim is not queue-delayed."""
    sample = _sample(problem, params)
    problem._audit_incast = sample
    ok = (
        sample["victim_rtt_ms"] is not None
        and sample["control_rtt_ms"] is not None
        and sample["queue_delay_ms"] is not None
        and not _delayed(sample)
    )
    return ok, sample


def incast_queueing(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Synchronized bursts fill the shallow egress queue: a victim flow through it
    is delayed by a large share of the queue's drain time; a control flow is not."""
    sample = _sample(problem, params)
    baseline = getattr(problem, "_audit_incast", None) or {}
    victim = sample["victim_rtt_ms"]
    control = sample["control_rtt_ms"]
    healthy_victim = baseline.get("victim_rtt_ms")
    healthy_control = baseline.get("control_rtt_ms")
    queue = sample["queue_delay_ms"]
    device, intf = problem._egress_port(params)
    qdiscs = []
    if device not in (problem.net_env.bmv2_switches or []):
        raw_qdisc = exec_or_empty(
            problem.runtime,
            device,
            f"tc -j -s qdisc show dev {shlex.quote(intf)}",
        )
        try:
            qdiscs = json.loads(raw_qdisc)
        except ValueError:
            pass
    drops = (
        next(
            (
                row["drops"]
                for row in qdiscs
                if isinstance(row, dict)
                and row.get("kind") == "tbf"
                and row.get("root")
                and isinstance(row.get("drops"), int)
            ),
            None,
        )
        if isinstance(qdiscs, list)
        else None
    )
    excess_delay = (
        (victim - control) - (healthy_victim - healthy_control)
        if None not in (victim, control, healthy_victim, healthy_control)
        else None
    )
    delayed = bool(
        healthy_victim and victim is not None and victim >= healthy_victim * _RTT_FACTOR
    )
    relative_delay = bool(
        queue
        and excess_delay is not None
        and excess_delay >= queue * _QUEUE_DELAY_SHARE
    )
    # TBF is installed fresh by the fault. Real drops on that egress port are
    # stronger evidence than RTT subtraction when the control is also busy.
    verified = delayed and (relative_delay or bool(drops and drops > 0))
    control_ok = (
        sample["control_rtt_ms"] is not None
        and not (sample["control_loss_percent"] or 0.0) > 20.0
    )
    return verified and control_ok, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified and control_ok,
        details={
            **sample,
            "baseline": baseline,
            "excess_delay_ms": excess_delay,
            "queue_drops": drops,
            "victim_delayed": verified,
            "control_ok": control_ok,
        },
    )
