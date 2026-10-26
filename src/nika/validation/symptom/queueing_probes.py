"""Symptom probes for egress-queue faults (test-path only)."""

from __future__ import annotations

import re
import time
from typing import Any

from nika.net_env.verify import ping_stats
from nika.problems.base import build_verify_result
from nika.problems.rca.inventory import iter_link_termination_points, parse_endpoint

_SAMPLE_PINGS = 30
_PING_INTERVAL_S = 0.2
# Incast tail-drops at the egress queue although the port is mostly idle: over
# the sampling window the qdisc drops packets while the average rate it sends
# stays below this share of the port rate. Sustained overload would instead
# keep the port busy for the whole window.
_MAX_MEAN_UTILIZATION = 0.6
# A control flow that avoids the egress port must stay healthy, so the drops
# are local to that port and not a fabric-wide forwarding overload.
_MAX_CONTROL_LOSS_PERCENT = 20.0
_QDISC_STATS_RE = re.compile(r"Sent (\d+) bytes (\d+) pkt \(dropped (\d+)")


def _qdisc_counters(problem: Any, device: str, intf: str) -> dict[str, int] | None:
    """Root qdisc byte, packet and drop counters on ``device:intf``."""
    output = problem.runtime.exec(device, f"tc -s qdisc show dev {intf}", timeout=10)
    match = _QDISC_STATS_RE.search(output)
    if match is None:
        return None
    sent_bytes, packets, drops = (int(group) for group in match.groups())
    return {"bytes": sent_bytes, "packets": packets, "drops": drops}


def _control(problem: Any, params: Any, device: str) -> tuple[str, str] | None:
    """A server or host the observer reaches without the egress port toward the
    receiver."""
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
    roles = getattr(problem.net_env, "servers", None) or {}
    servers = [*(roles.get("web") or []), *(roles.get("dns") or [])]
    hosts = getattr(problem.net_env, "hosts", None) or []
    skip = {params.host_name, params.observer_device, device}
    # Prefer a destination off the egress device and off the observer's own
    # switch, so the control crosses the fabric like the victim does. A host on
    # another port of the egress device still avoids the shallow queue.
    candidates = sorted(
        dict.fromkeys([*servers, *hosts]), key=lambda n: (n in attached, n in near)
    )
    for name in candidates:
        if name in skip:
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
    """Egress qdisc drops and mean send rate over a window spanning two ping
    flows: a victim flow through the egress port and a control flow that avoids it.
    """
    device, intf = problem._egress_port(params)
    observer = params.observer_device
    receiver_ip = _receiver_ip(problem, params)
    control = _control(problem, params, device)
    before, started = _qdisc_counters(problem, device, intf), time.monotonic()
    victim = _flow(problem, observer, receiver_ip) if observer and receiver_ip else None
    control_stats = (
        _flow(problem, observer, control[1]) if observer and control else None
    )
    after, window = _qdisc_counters(problem, device, intf), time.monotonic() - started
    shape, _tc_output = problem.port_shape(device, intf)
    port_rate = shape["rate_bps"] if shape else None
    drops = mean_rate = None
    if before is not None and after is not None:
        drops = after["drops"] - before["drops"]
        mean_rate = (after["bytes"] - before["bytes"]) * 8 / window
    return {
        "egress": f"{device}:{intf}",
        "window_s": round(window, 2),
        "drops_delta": drops,
        "mean_rate_bps": round(mean_rate) if mean_rate is not None else None,
        "port_rate_bps": port_rate,
        "mean_utilization": (
            round(mean_rate / port_rate, 3)
            if mean_rate is not None and port_rate
            else None
        ),
        "observer": observer,
        "receiver_ip": receiver_ip,
        "victim_rtt_ms": getattr(victim, "rtt_avg_ms", None),
        "victim_loss_percent": getattr(victim, "loss_percent", None),
        "control": control[0] if control else None,
        "control_rtt_ms": getattr(control_stats, "rtt_avg_ms", None),
        "control_loss_percent": getattr(control_stats, "loss_percent", None),
    }


def _control_ok(sample: dict[str, Any]) -> bool:
    return (
        sample["control_rtt_ms"] is not None
        and (sample["control_loss_percent"] or 0.0) <= _MAX_CONTROL_LOSS_PERCENT
    )


def incast_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Before the fan-in both flows answer and the egress queue drops nothing."""
    sample = _sample(problem, params)
    ok = (
        sample["victim_rtt_ms"] is not None
        and _control_ok(sample)
        and not sample["drops_delta"]
    )
    return ok, sample


def incast_queueing(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Synchronized fan-in overflows the shallow egress queue: the port tail-drops
    while its average send rate stays well below the port rate, and a control
    flow that avoids the port stays healthy."""
    sample = _sample(problem, params)
    utilization = sample["mean_utilization"]
    tail_drops = bool(sample["drops_delta"])
    below_rate = utilization is not None and utilization < _MAX_MEAN_UTILIZATION
    control_ok = _control_ok(sample)
    verified = tail_drops and below_rate and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **sample,
            "tail_drops": tail_drops,
            "below_port_rate": below_rate,
            "control_ok": control_ok,
        },
    )
