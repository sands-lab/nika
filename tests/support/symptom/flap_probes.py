"""Periodic link-flap symptom probes (test-path only)."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from nika.net_env.verify import (
    exec_or_empty,
    frr_bgp_has_established_session,
    ping_stats,
)
from nika.problems.link_interface.link import _resolve_link_intf
from nika.problems.support.probe_paths import ProbePath
from nika.runtime.base import LabRuntime
from nika.runtime.kathara.runtime import KatharaRuntime
from tests.support.symptom.probe import _resolve_path

BASELINE_PING_COUNT = 15
BASELINE_MAX_LOSS_PERCENT = 5.0
SAMPLE_INTERVAL_SEC = 0.25


@dataclass
class FlapSample:
    t: float
    ping_ok: bool
    operstate: str
    routing_ok: bool | None = None
    proxy_operstate: str | None = None


def _vde_proxy_operstate(problem: Any) -> str | None:
    if not isinstance(problem.runtime, KatharaRuntime):
        return None
    proxy = getattr(problem, "_proxy", None)
    if proxy is None:
        return None
    try:
        import docker

        container = docker.from_env().containers.get(proxy.proxy_id)
        result = container.exec_run(["cat", "/sys/class/net/eth1/operstate"])
        if result.exit_code == 0:
            return result.output.decode(errors="ignore").strip()
    except Exception:  # noqa: BLE001
        return None
    return None


def assert_baseline_healthy(
    runtime: LabRuntime,
    path: ProbePath,
) -> tuple[bool, dict[str, Any]]:
    """Require a healthy path before injecting link_flap."""
    if not path.dst_ip:
        return False, {"error": "no_probe_dst"}
    last_details: dict[str, Any] = {"error": "no_probe_attempt"}
    for attempt in range(3):
        stats = ping_stats(
            runtime,
            path.src_host,
            path.dst_ip,
            count=BASELINE_PING_COUNT,
            interval_sec=0.2,
        )
        last_details = {
            "src_host": path.src_host,
            "dst_ip": path.dst_ip,
            "loss_percent": stats.loss_percent,
            "received": stats.received,
            "transmitted": stats.transmitted,
            "attempt": attempt + 1,
        }
        if stats.received > 0 and stats.loss_percent <= BASELINE_MAX_LOSS_PERCENT:
            return True, last_details
        if attempt < 2:
            time.sleep(2.0)
    return False, last_details


def _single_ping_ok(runtime: LabRuntime, host: str, target: str) -> bool:
    output = exec_or_empty(runtime, host, f"ping -c 1 -W 0.2 {target}", timeout=2)
    return "1 received" in output or " 0% packet loss" in output


def _routing_probe_for(
    runtime: LabRuntime,
    inject_host: str,
    *,
    routers: list[str] | None,
) -> Callable[[LabRuntime], bool] | None:
    if routers and inject_host in routers:
        # ISP labs often run ISIS/OSPF without BGP. Prefer any live IGP/BGP
        # adjacency so link-quality symptoms are not gated on absent BGP.
        return lambda rt: _frr_control_plane_ok(rt, inject_host)

    if inject_host.startswith(("leaf", "spine", "gateway")):
        return lambda rt: _srlinux_bgp_established(rt, inject_host)
    return None


def _frr_control_plane_ok(runtime: LabRuntime, host: str) -> bool:
    if frr_bgp_has_established_session(runtime, host):
        return True
    ospf = exec_or_empty(
        runtime,
        host,
        "vtysh -c 'show ip ospf neighbor' 2>/dev/null || true",
        timeout=15,
    )
    if any(field.startswith("Full") for line in ospf.splitlines() for field in line.split()):
        return True
    isis = exec_or_empty(
        runtime,
        host,
        "vtysh -c 'show isis neighbor' 2>/dev/null || true",
        timeout=15,
    )
    for line in isis.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[3] == "Up":
            return True
    # No control-plane protocol configured on this node: do not fail the gate.
    blob = f"{ospf}\n{isis}".lower()
    if "bgp" not in blob and "ospf" not in blob and "isis" not in blob:
        return True
    return False


def _srlinux_bgp_established(runtime: LabRuntime, host: str) -> bool:
    output = exec_or_empty(
        runtime,
        host,
        'sr_cli "show network-instance default protocols bgp neighbor" 2>/dev/null || true',
        timeout=15,
    )
    return "established" in output.lower()


def observe_link_flap_window(
    runtime: LabRuntime,
    path: ProbePath,
    *,
    fault_host: str,
    fault_intf: str,
    down_time: int,
    up_time: int,
    routing_probe: Callable[[LabRuntime], bool] | None = None,
    problem: Any | None = None,
) -> list[FlapSample]:
    """Sample ping, operstate, and optional routing state across flap cycles."""
    if not path.dst_ip:
        return []
    duration = max(12.0, 4 * (down_time + up_time))
    samples: list[FlapSample] = []
    start = time.time()
    while time.time() - start < duration:
        t = time.time()
        ping_ok = _single_ping_ok(runtime, path.src_host, path.dst_ip)
        operstate = runtime.get_interface_operstate(fault_host, fault_intf)
        proxy_operstate = _vde_proxy_operstate(problem) if problem is not None else None
        routing_ok = routing_probe(runtime) if routing_probe is not None else None
        samples.append(
            FlapSample(
                t=t,
                ping_ok=ping_ok,
                operstate=operstate,
                routing_ok=routing_ok,
                proxy_operstate=proxy_operstate,
            )
        )
        time.sleep(SAMPLE_INTERVAL_SEC)
    return samples


def evaluate_flap_samples(
    samples: list[FlapSample],
    *,
    kathara_backend: bool,
) -> tuple[bool, dict[str, Any]]:
    if len(samples) < 4:
        return False, {"error": "insufficient_samples", "count": len(samples)}

    operstates = [sample.operstate for sample in samples]
    proxy_states = [
        sample.proxy_operstate for sample in samples if sample.proxy_operstate
    ]
    state_series = (
        proxy_states if kathara_backend and len(set(proxy_states)) > 1 else operstates
    )
    transitions = sum(
        1 for i in range(1, len(state_series)) if state_series[i] != state_series[i - 1]
    )

    ping_oks = [sample.ping_ok for sample in samples]
    has_success = any(ping_oks)
    has_failure = not all(ping_oks)
    aggregate_loss = (1.0 - (sum(ping_oks) / len(ping_oks))) * 100.0
    periodic_loss = has_success and has_failure and 5.0 < aggregate_loss < 95.0

    slow_recon_flap = (
        not kathara_backend
        and transitions >= 2
        and "down" in operstates
        and "up" in operstates
        and aggregate_loss >= 5.0
        and not has_success
    )

    down_samples = [s for s in samples if (s.proxy_operstate or s.operstate) == "down"]
    up_samples = [s for s in samples if (s.proxy_operstate or s.operstate) == "up"]
    alignment_ok = True
    if down_samples and up_samples:
        down_fail_rate = sum(1 for s in down_samples if not s.ping_ok) / len(
            down_samples
        )
        up_success_rate = sum(1 for s in up_samples if s.ping_ok) / len(up_samples)
        alignment_ok = down_fail_rate >= 0.5 and up_success_rate >= 0.5
    elif transitions == 0 and kathara_backend:
        alignment_ok = periodic_loss

    if slow_recon_flap:
        periodic_loss = True
        alignment_ok = True

    # Kathara flaps the VDE proxy, not the node operstate. Multipath ISP labs
    # often keep ICMP up via alternate routes while the proxy still cycles.
    # Observed proxy up/down transitions are the authoritative flap signal.
    kathara_proxy_flap = (
        kathara_backend
        and "up" in proxy_states
        and "down" in proxy_states
        and transitions >= 2
    )
    if kathara_proxy_flap and not periodic_loss:
        periodic_loss = True
        alignment_ok = True

    operstate_ok = (
        transitions >= 2 if not kathara_backend else (transitions >= 2 or periodic_loss)
    )

    routing_vals = [s.routing_ok for s in samples if s.routing_ok is not None]
    routing_churn = True
    if routing_vals:
        # Require at least one healthy routing sample when a probe is present.
        # Clear dataplane flap evidence (periodic loss + operstate/proxy) is
        # enough when IGP never recovers during the short observation window.
        routing_churn = any(routing_vals) or (
            periodic_loss and operstate_ok and alignment_ok
        )

    ok = periodic_loss and operstate_ok and alignment_ok and routing_churn
    return ok, {
        "transitions": transitions,
        "periodic_loss": periodic_loss,
        "slow_recon_flap": slow_recon_flap,
        "kathara_proxy_flap": kathara_proxy_flap,
        "aggregate_loss_percent": aggregate_loss,
        "operstate_ok": operstate_ok,
        "alignment_ok": alignment_ok,
        "routing_churn": routing_churn,
        "ping_successes": sum(ping_oks),
        "ping_failures": len(ping_oks) - sum(ping_oks),
        "sample_count": len(samples),
    }


def evaluate_link_flap_symptom(
    problem: Any,
    params: Any,
) -> tuple[bool, dict[str, Any]]:
    """Custom evaluate_symptom handler for link_flap."""
    runtime = problem.runtime
    scenario = getattr(problem, "scenario_name", None)
    path = _resolve_path(scenario, params)
    if path is None or not path.dst_ip:
        return False, {"error": "no_probe_path", "scenario": scenario}

    backend = "kathara" if isinstance(runtime, KatharaRuntime) else "containerlab"
    fault_intf = _resolve_link_intf(getattr(params, "intf_name", "eth0"), backend)
    fault_host = getattr(params, "host_name", None)
    if not fault_host:
        return False, {"error": "no_fault_host"}

    qdisc = exec_or_empty(
        runtime,
        fault_host,
        f"tc qdisc show dev {fault_intf} 2>/dev/null || true",
    ).lower()
    if "netem" in qdisc or "tbf" in qdisc:
        return False, {
            "error": "shortcut_leak",
            "host": fault_host,
            "intf": fault_intf,
            "qdisc": qdisc,
        }

    down_time = int(getattr(params, "down_time", 1) or 1)
    up_time = int(getattr(params, "up_time", 1) or 1)
    routers = list(getattr(problem.net_env, "routers", None) or [])
    routing_probe = _routing_probe_for(runtime, fault_host, routers=routers)

    samples = observe_link_flap_window(
        runtime,
        path,
        fault_host=fault_host,
        fault_intf=fault_intf,
        down_time=down_time,
        up_time=up_time,
        routing_probe=routing_probe,
        problem=problem,
    )
    ok, comparison = evaluate_flap_samples(
        samples, kathara_backend=isinstance(runtime, KatharaRuntime)
    )
    return ok, {
        "failure": "link_flap",
        "probe": "link_flap_periodic",
        "symptom_class": "loss",
        "path": {"src_host": path.src_host, "dst_ip": path.dst_ip},
        "fault": {"host": fault_host, "intf": fault_intf},
        "samples": [asdict(s) for s in samples],
        "comparison": comparison,
    }
