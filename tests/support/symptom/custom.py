"""Custom evaluate_symptom handlers for failures with domain-specific gates."""

from __future__ import annotations

import re
import time
from typing import Any

from nika.net_env.verify import (
    http_download_stats,
    iperf_throughput_bps,
    median_float,
    ping_stats,
)
from nika.problems.base import build_verify_result
from nika.problems.support.ab_helpers import ab_summary_to_dict
from tests.support.symptom.addressing_probes import (
    ip_conflict,
    ip_conflict_baseline,
    mac_conflict,
    mac_conflict_baseline,
)
from tests.support.symptom.flap_probes import evaluate_link_flap_symptom
from tests.support.symptom.icmp_probes import (
    frag_needed_baseline,
    frag_needed_filtered,
)
from tests.support.symptom.p4_gateway_probes import (
    ecn_marking,
    ecn_marking_baseline,
    int_headroom,
    int_headroom_baseline,
    lb_race,
    lb_race_baseline,
    silent_loss,
    silent_loss_baseline,
    syn_flood,
    syn_flood_baseline,
    tcam_drop,
    tcam_drop_baseline,
)
from tests.support.symptom.sdn_probes import flow_shadow, flow_shadow_baseline
from tests.support.symptom.nat_probes import (
    nat_flow_baseline,
    nat_mapping_removed,
    snat_pool_baseline,
    snat_pool_exhaustion,
)
from tests.support.symptom.corruption_probes import (
    evaluate_device_forwarding_corruption_symptom,
    evaluate_link_capacity_symptom,
    evaluate_link_corruption_symptom,
)
from nika.problems.service_networking.load_balancer import (
    _BACKEND_CPU_MAX_RATIO,
    _BACKEND_LOCAL_URL,
    _CONTROL_VS_VIP_MAX_RATIO,
    _MAX_LOSS_PERCENT as _LB_MAX_LOSS_PERCENT,
    _MIN_ERROR_COUNT_FOR_DEGRADATION,
    _NGINX_CPU_MIN_RATIO,
    _VIP_PING_HOST,
    _VIP_TAIL_MIN_RATIO,
)
from nika.problems.endpoint_application.transport import (
    _MAX_LOSS_PERCENT as _SENDER_MAX_LOSS_PERCENT,
    _THROUGHPUT_MAX_RATIO,
    _TIME_MIN_RATIO,
)


def _web_dos(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    target_ip = problem.runtime.get_host_ip(params.host_name, with_prefix=False)
    after = problem._http_samples(params, target_ip)
    baseline = problem._baseline
    degradation_ok: bool | None = None
    latency_ratio: float | None = None
    if baseline is not None:
        before_p95 = baseline.get("p95_ms")
        after_p95 = after.get("p95_ms")
        before_median = baseline.get("median_ms")
        after_median = after.get("median_ms")
        if before_p95 and after_p95 is not None:
            latency_ratio = float(after_p95) / float(before_p95)
        error_degraded = bool(
            baseline["error_rate"] <= 0.2 and after["error_rate"] >= 0.4
        )
        latency_degraded = bool(
            before_p95 is not None
            and after_p95 is not None
            and float(after_p95) >= float(before_p95) * 5.0
            and float(after_p95) - float(before_p95) >= 25.0
        )
        median_degraded = bool(
            before_median is not None
            and after_median is not None
            and float(after_median) >= float(before_median) * 5.0
            and float(after_median) - float(before_median) >= 25.0
        )
        degradation_ok = error_degraded or latency_degraded or median_degraded

    verified = degradation_ok is True
    result = build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "baseline": baseline,
            "after": after,
            "latency_ratio": latency_ratio,
            "degradation_ok": degradation_ok,
        },
    )
    return verified, result


def _sender_resource_contention(
    problem: Any, params: Any
) -> tuple[bool, dict[str, Any]]:
    ping = ping_stats(
        problem.runtime,
        params.client_host,
        params.dst_ip,
        count=10,
        interval_sec=0.2,
    )
    small = http_download_stats(
        problem.runtime,
        params.client_host,
        params.small_url,
        max_time_sec=30,
    )
    injected_bps, injected_time_s, probe_timeout_sec = (
        problem.measure_fault_degradation(params)
    )

    baseline_bps = problem._baseline_throughput_bps
    baseline_time = problem._baseline_time_s
    baseline_rtt = problem._baseline_rtt_ms

    # Path gate: loss + small HTTP. Do not require ICMP RTT≈baseline — the
    # contended host replies to ping with the same CFS-starved CPU, so RTT
    # inflation is an endpoint symptom, not a path fault.
    path_ok = (
        ping.loss_percent is not None
        and ping.loss_percent <= _SENDER_MAX_LOSS_PERCENT
        and small.ok
    )

    throughput_ratio = None
    time_ratio = None
    perf_ok = False
    if (
        baseline_bps
        and baseline_time
        and injected_bps is not None
        and injected_time_s is not None
    ):
        throughput_ratio = injected_bps / baseline_bps
        time_ratio = injected_time_s / baseline_time
        perf_ok = (
            throughput_ratio <= _THROUGHPUT_MAX_RATIO or time_ratio >= _TIME_MIN_RATIO
        )

    verified = bool(path_ok and perf_ok)
    result = build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "host": params.host_name,
            "client_host": params.client_host,
            "path_ok": path_ok,
            "perf_ok": perf_ok,
            "small_http_ok": small.ok,
            "ping_rtt_ms": ping.rtt_avg_ms,
            "ping_loss_percent": ping.loss_percent,
            "baseline_throughput_bps": baseline_bps,
            "baseline_time_s": baseline_time,
            "baseline_rtt_ms": baseline_rtt,
            "injected_throughput_bps": injected_bps,
            "injected_time_s": injected_time_s,
            "throughput_ratio": throughput_ratio,
            "time_ratio": time_ratio,
            "probe_timeout_sec": probe_timeout_sec,
        },
    )
    return verified, result


def _load_balancer_overload(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    baseline = problem._baseline or {}
    load_hosts = problem._load_hosts or problem._parse_load_hosts(params)

    lb_cpu = problem._cpu_ratio_of_quota(
        params.host_name,
        quota_cpus=problem._applied_quota(params),
        sample_sec=params.cpu_sample_sec,
    )
    backend_cpu = problem._cpu_ratio_of_quota(
        params.backend_cpu_host,
        quota_cpus=0.5,
        sample_sec=params.cpu_sample_sec,
    )

    vip = problem._run_ab(
        params.client_host,
        params.vip_url,
        requests=params.probe_requests,
        concurrency=params.probe_concurrency,
        timeout_sec=params.probe_timeout_sec,
    )
    control = problem._run_ab(
        params.client_host,
        params.control_url,
        requests=params.probe_requests,
        concurrency=params.probe_concurrency,
        timeout_sec=params.probe_timeout_sec,
    )
    backend_from_lb = http_download_stats(
        problem.runtime,
        params.backend_probe_host,
        params.backend_url,
        max_time_sec=min(30, params.probe_timeout_sec),
        connect_timeout_sec=5,
    )
    backend_local = http_download_stats(
        problem.runtime,
        params.backend_cpu_host,
        _BACKEND_LOCAL_URL,
        max_time_sec=min(30, params.probe_timeout_sec),
        connect_timeout_sec=5,
    )
    ping = ping_stats(
        problem.runtime,
        params.client_host,
        _VIP_PING_HOST,
        count=10,
        interval_sec=0.2,
    )

    base_vip = baseline.get("vip") or {}
    base_control = baseline.get("control") or {}
    base_p95 = base_vip.get("p95_ms")
    base_p99 = base_vip.get("p99_ms")
    base_control_p95 = base_control.get("p95_ms")
    base_rtt = baseline.get("ping_rtt_ms")

    p95_ratio = (vip.p95_ms / base_p95) if vip.p95_ms is not None and base_p95 else None
    p99_ratio = (vip.p99_ms / base_p99) if vip.p99_ms is not None and base_p99 else None
    control_p95_ratio = (
        (control.p95_ms / base_control_p95)
        if control.p95_ms is not None and base_control_p95
        else None
    )

    nginx_running = problem._nginx_running(params.host_name)
    nginx_saturated = lb_cpu is not None and lb_cpu >= _NGINX_CPU_MIN_RATIO
    vip_tail_ok = (p95_ratio is not None and p95_ratio >= _VIP_TAIL_MIN_RATIO) or (
        p99_ratio is not None and p99_ratio >= _VIP_TAIL_MIN_RATIO
    )
    vip_errors_ok = vip.error_count >= _MIN_ERROR_COUNT_FOR_DEGRADATION
    vip_probe_failed = vip.p95_ms is None or vip.complete_requests in {None, 0}
    vip_degraded = vip_tail_ok or vip_errors_ok or vip_probe_failed

    control_ok = (
        control.p95_ms is not None
        and control.error_count == 0
        and vip.p95_ms is not None
        and control.p95_ms <= vip.p95_ms * _CONTROL_VS_VIP_MAX_RATIO
    )
    base_backend_local_time = baseline.get("backend_local_time_s")
    backend_local_time_ratio = None
    if (
        backend_local.ok
        and backend_local.time_total_s is not None
        and base_backend_local_time
        and base_backend_local_time > 0
    ):
        backend_local_time_ratio = backend_local.time_total_s / float(
            base_backend_local_time
        )
    backend_ok = (
        backend_from_lb.ok
        and backend_local.ok
        and (backend_cpu is None or backend_cpu <= _BACKEND_CPU_MAX_RATIO)
    )
    path_ok = (
        ping.loss_percent is not None
        and ping.loss_percent <= _LB_MAX_LOSS_PERCENT
        and nginx_running
    )
    # ICMP replies come from the CPU-limited VIP host itself. Its RTT is an
    # endpoint load signal, not an independent fabric-path health gate.

    verified = bool(
        nginx_saturated and vip_degraded and control_ok and backend_ok and path_ok
    )
    result = build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "host": params.host_name,
            "client_host": params.client_host,
            "load_hosts": load_hosts,
            "nginx_running": nginx_running,
            "nginx_saturated": nginx_saturated,
            "lb_cpu_ratio": lb_cpu,
            "backend_cpu_ratio": backend_cpu,
            "active_connections": problem._active_connections(params.host_name),
            "vip": ab_summary_to_dict(vip),
            "control": ab_summary_to_dict(control),
            "backend_from_lb_ok": backend_from_lb.ok,
            "backend_from_lb_time_s": backend_from_lb.time_total_s,
            "backend_local_ok": backend_local.ok,
            "backend_local_time_s": backend_local.time_total_s,
            "baseline_vip_p95_ms": base_p95,
            "baseline_vip_p99_ms": base_p99,
            "baseline_control_p95_ms": base_control_p95,
            "p95_ratio": p95_ratio,
            "p99_ratio": p99_ratio,
            "control_p95_ratio": control_p95_ratio,
            "backend_local_time_ratio": backend_local_time_ratio,
            "vip_tail_ok": vip_tail_ok,
            "vip_errors_ok": vip_errors_ok,
            "vip_degraded": vip_degraded,
            "control_ok": control_ok,
            "backend_ok_gate": backend_ok,
            "path_ok": path_ok,
            "ping_rtt_ms": ping.rtt_avg_ms,
            "ping_loss_percent": ping.loss_percent,
            "baseline_rtt_ms": base_rtt,
            "baseline": baseline,
        },
    )
    return verified, result


def _lb_connection_state_exhaustion(
    problem: Any, params: Any
) -> tuple[bool, dict[str, Any]]:
    from nika.net_env.verify import http_ok

    profile = getattr(problem, "_affinity_profile", None) or {}
    client_host = profile.get("client_host") or params.client_host
    status_path = profile.get("status_path", "/tmp/nika-lb-conn.status")
    pid_path = profile.get("pid_path", "/tmp/nika-lb-conn.pid")
    backend_dip = profile.get("backend_dip") or params.backend_dip
    vip_url = profile.get("vip_url") or params.vip_url

    status = problem.runtime.exec(
        client_host, f"cat {status_path} 2>/dev/null || true"
    ).strip()
    deadline = time.time() + 8.0
    while time.time() < deadline and status not in {"broken", "ok"}:
        time.sleep(0.5)
        status = problem.runtime.exec(
            client_host, f"cat {status_path} 2>/dev/null || true"
        ).strip()
    if status != "broken":
        running = problem.runtime.exec(
            client_host,
            f"kill -0 $(cat {pid_path} 2>/dev/null) 2>/dev/null && echo running || echo dead",
        ).strip()
        if running == "running":
            problem.runtime.exec(
                client_host,
                f"kill $(cat {pid_path} 2>/dev/null) 2>/dev/null || true",
            )
            time.sleep(1.0)
            status = problem.runtime.exec(
                client_host, f"cat {status_path} 2>/dev/null || true"
            ).strip()

    affinity_broken = status == "broken"
    vip_ok = http_ok(problem.runtime, client_host, vip_url)
    verified = affinity_broken and vip_ok
    result = build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "affinity_status": status,
            "affinity_broken": affinity_broken,
            "vip_ok": vip_ok,
            "backend_dip": backend_dip,
            "vip_url": vip_url,
        },
    )
    return verified, result


def _receiver_resource_contention(
    problem: Any, params: Any
) -> tuple[bool, dict[str, Any]]:
    url = getattr(problem, "_large_url", None) or getattr(params, "large_url", None)
    if not url:
        url = problem._resolve_large_url(params)
    baseline_bps = problem._baseline_throughput_bps
    baseline_time = problem._baseline_time_s
    # A starved receiver can stall a download, or the exec that starts it, for
    # minutes. Count a missed deadline as the measured time and stop at the
    # first slow sample so all reads finish inside the stress duration.
    max_time_sec = max(20, int((baseline_time or 5) * 4) + 1)
    rates: list[float] = []
    times: list[float] = []
    for _ in range(3):
        stats = http_download_stats(
            problem.runtime, params.host_name, url, max_time_sec=max_time_sec
        )
        if stats.raw.startswith("[TIMEOUT]"):
            rates.append(0.0)
            times.append(float(max_time_sec))
        elif stats.throughput_bps is not None:
            rates.append(stats.throughput_bps)
            times.append(stats.time_total_s)
        elif (stats.time_total_s or 0) >= max_time_sec:
            rates.append((stats.size_bytes or 0) * 8.0 / max_time_sec)
            times.append(stats.time_total_s)
        else:
            continue
        if baseline_time and times[-1] >= 2.0 * baseline_time:
            break
    injected_bps, injected_time_s = median_float(rates), median_float(times)
    throughput_ratio = None
    time_ratio = None
    perf_ok = False
    if (
        baseline_bps
        and baseline_time
        and injected_bps is not None
        and injected_time_s is not None
    ):
        throughput_ratio = injected_bps / baseline_bps
        time_ratio = injected_time_s / baseline_time
        # Receiver contention: clear slowdown, not necessarily sender-class multi-x.
        perf_ok = throughput_ratio <= 0.50 or time_ratio >= 2.0
    elif baseline_bps and baseline_time and injected_bps is None:
        # Contended receiver may fail large downloads entirely.
        perf_ok = True
    verified = bool(perf_ok)
    result = build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "host": params.host_name,
            "large_url": url,
            "perf_ok": perf_ok,
            "baseline_throughput_bps": baseline_bps,
            "baseline_time_s": baseline_time,
            "injected_throughput_bps": injected_bps,
            "injected_time_s": injected_time_s,
            "throughput_ratio": throughput_ratio,
            "time_ratio": time_ratio,
        },
    )
    return verified, result


def _tcp_receive_window_limited(
    problem: Any, params: Any
) -> tuple[bool, dict[str, Any]]:
    from nika.problems.traffic_queueing_resource.tcp_rwnd_helpers import primary_ipv4

    receiver_ip = primary_ipv4(problem.runtime, params.host_name)
    baseline_bps = getattr(problem, "_healthy_throughput_bps", None)
    current_bps = (
        iperf_throughput_bps(
            problem.runtime,
            params.sender_host,
            params.host_name,
            receiver_ip,
            duration_sec=3,
        )
        if receiver_ip
        else None
    )
    small = http_download_stats(
        problem.runtime, params.host_name, params.small_url, max_time_sec=30
    )
    ping = ping_stats(
        problem.runtime, params.host_name, params.sender_ip, count=5, interval_sec=0.2
    )
    ratio = (
        current_bps / baseline_bps if current_bps is not None and baseline_bps else None
    )
    path_ok = small.ok and ping.loss_percent < 5.0
    verified = bool(path_ok and ratio is not None and ratio < 0.5)
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "receiver": params.host_name,
            "receiver_ip": receiver_ip,
            "small_http_ok": small.ok,
            "ping_loss_percent": ping.loss_percent,
            "healthy_throughput_bps": baseline_bps,
            "current_throughput_bps": current_bps,
            "throughput_ratio": ratio,
            "path_ok": path_ok,
        },
    )


def _vrf_dscp_remarking(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    observed = problem.verify_fault(params)
    details = observed.get("details") if isinstance(observed, dict) else {}
    details = details if isinstance(details, dict) else {}
    smoke = details.get("smoke") if isinstance(details.get("smoke"), dict) else {}
    verified = bool(
        details.get("samples_confirm")
        and details.get("perf_degraded")
        and smoke
        and all(smoke.values())
    )
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details=details,
    )


def _southbound_disconnected(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    model = getattr(problem.net_env, "model", None)
    switches = list(getattr(model, "leaves", []) or []) + list(
        getattr(model, "spines", []) or []
    )
    states: dict[str, bool] = {}
    deadline = time.monotonic() + 90.0
    while time.monotonic() < deadline:
        states = {}
        for switch in switches:
            output = problem.runtime.exec(
                switch,
                "ovs-vsctl --format=csv --no-headings --columns=is_connected "
                "list Controller 2>/dev/null || true",
                timeout=10,
            ).strip()
            states[switch] = "true" in output.lower()
        if states and not any(states.values()):
            break
        time.sleep(2.0)
    disconnected = sorted(name for name, connected in states.items() if not connected)
    verified = bool(states) and len(disconnected) == len(states)
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={"switch_connected": states, "disconnected": disconnected},
    )


def _clusterip_routing_broken(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    result = problem.verify_fault(params)
    details = result.get("details") or {}
    verified = (
        bool(result.get("verified"))
        and details.get("clusterip_reachable") is False
        and details.get("backend_reachable") is True
        and details.get("service_object_intact") is True
    )
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details=details,
    )


def _flow_rule_loop(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Exercise all remote racks and tie packet loss to the loop rule counters."""
    model = problem.net_env.model
    source_leaf = model.leaf_id(params.host_name)
    sources = [e for e in model.client_endpoints() if e.leaf_id == source_leaf]
    targets = [e for e in model.web_endpoints() if e.leaf_id != source_leaf]
    if not sources or not targets:
        return False, {"error": "no_cross_leaf_probe_path"}

    port0, port1 = problem._resolve_ports(params)
    switches = ((params.host_name, port0), (params.host_name_2, port1))

    def loop_packets(switch: str, port: str) -> int | None:
        ofport = problem.runtime.exec(
            switch, f"ovs-vsctl get Interface {port} ofport 2>/dev/null"
        ).strip()
        flows = problem.runtime.exec(
            switch, f"ovs-ofctl -O OpenFlow13 dump-flows {switch} 2>/dev/null"
        )
        for line in flows.splitlines():
            if f"priority={problem._LOOP_PRIORITY},in_port={ofport}" in line:
                match = re.search(r"\bn_packets=(\d+)", line)
                if match:
                    return int(match.group(1))
        return None

    before = {switch: loop_packets(switch, port) for switch, port in switches}
    losses: dict[str, float] = {}
    for target in targets:
        sample = ping_stats(
            problem.runtime, sources[0].name, target.ip, count=3, interval_sec=0.2
        )
        losses[target.name] = sample.loss_percent
    after = {switch: loop_packets(switch, port) for switch, port in switches}
    counter_rise = any(
        before[switch] is not None
        and after[switch] is not None
        and after[switch] > before[switch]
        for switch, _ in switches
    )
    verified = counter_rise and any(loss > 0 for loss in losses.values())
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "source": sources[0].name,
            "loss_percent_by_target": losses,
            "loop_packets_before": before,
            "loop_packets_after": after,
        },
    )


_QDISC_DROPPED_RE = re.compile(r"\bdropped (\d+)")
_INCAST_SAMPLE_SEC = 5.0


def _egress_qdisc_drops(problem: Any, device: str, intf: str) -> tuple[int | None, str]:
    output = problem.runtime.exec(
        device,
        f"tc -s qdisc show dev {intf} 2>/dev/null || true; "
        f"tc -s filter show dev {intf} ingress 2>/dev/null || true",
        timeout=10,
    )
    counts = [int(m) for m in _QDISC_DROPPED_RE.findall(output or "")]
    return (sum(counts) if counts else None), output


def _incast(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Synchronized bursts overflow the shallow egress queue: drops keep rising."""
    device, intf = problem._egress_port(params)
    before, before_stats = _egress_qdisc_drops(problem, device, intf)
    time.sleep(_INCAST_SAMPLE_SEC)
    after, after_stats = _egress_qdisc_drops(problem, device, intf)
    delta = after - before if before is not None and after is not None else None
    verified = delta is not None and delta > 0
    diagnostics: dict[str, str] = {}
    if not verified:
        senders = list(getattr(problem, "_senders", []) or [])
        if senders:
            diagnostics["sender_log"] = problem.runtime.exec(
                senders[0],
                'for f in /tmp/burst-*.log; do tail -n 8 "$f"; done',
                timeout=10,
            )[:1200]
            diagnostics["sender_process"] = problem.runtime.exec(
                senders[0], "pgrep -af iperf3 2>/dev/null || true", timeout=10
            )[:800]
            destination_ip = str(getattr(problem, "_receiver_ip", ""))
            diagnostics["sender_route"] = problem.runtime.exec(
                senders[0],
                f"ip route get {destination_ip} 2>/dev/null || true",
                timeout=10,
            )[:500]
        diagnostics["receiver_log"] = problem.runtime.exec(
            params.host_name,
            'for f in /tmp/burst-server-*.log; do tail -n 8 "$f"; done',
            timeout=10,
        )[:1200]
        diagnostics["receiver_process"] = problem.runtime.exec(
            params.host_name, "pgrep -af iperf3 2>/dev/null || true", timeout=10
        )[:800]
        diagnostics["receiver_ip"] = str(getattr(problem, "_receiver_ip", ""))
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "egress": f"{device}:{intf}",
            "drops_before": before,
            "drops_after": after,
            "drops_delta": delta,
            "window_sec": _INCAST_SAMPLE_SEC,
            "tc_stats_before": before_stats[:1200],
            "tc_stats_after": after_stats[:1200],
            **diagnostics,
        },
    )


_CUSTOM: dict[str, Any] = {
    "incast_traffic_network_limitation": _incast,
    "link_flap": evaluate_link_flap_symptom,
    "link_packet_corruption": evaluate_link_corruption_symptom,
    "link_capacity_bottleneck": evaluate_link_capacity_symptom,
    "device_forwarding_packet_corruption": evaluate_device_forwarding_corruption_symptom,
    "web_dos_attack": _web_dos,
    "sender_resource_contention": _sender_resource_contention,
    "receiver_resource_contention": _receiver_resource_contention,
    "tcp_receive_window_limited": _tcp_receive_window_limited,
    "vrf_dscp_remarking": _vrf_dscp_remarking,
    "southbound_port_block": _southbound_disconnected,
    "southbound_port_mismatch": _southbound_disconnected,
    "flow_rule_loop": _flow_rule_loop,
    "k8s_clusterip_routing_broken": _clusterip_routing_broken,
    "silent_egress_packet_loss": silent_loss,
    "load_balancer_overload": _load_balancer_overload,
    "lb_connection_state_exhaustion": _lb_connection_state_exhaustion,
    "snat_port_pool_exhaustion": snat_pool_exhaustion,
    "nat_mapping_removed_without_drain": nat_mapping_removed,
    "mac_address_conflict": mac_conflict,
    "host_ip_conflict": ip_conflict,
    "icmp_frag_needed_filter_misconfiguration": frag_needed_filtered,
    "int_insufficient_mtu_headroom": int_headroom,
    "tcp_syn_flood_attack": syn_flood,
    "lb_pending_connection_update_race": lb_race,
    "flow_rule_shadowing": flow_shadow,
    "p4_ecn_threshold_misconfiguration": ecn_marking,
    "p4_tcam_entry_corruption": tcam_drop,
}

# Pre-inject measurements of the same signal a custom probe reads after inject.
_CUSTOM_BASELINE: dict[str, Any] = {
    "snat_port_pool_exhaustion": snat_pool_baseline,
    "nat_mapping_removed_without_drain": nat_flow_baseline,
    "mac_address_conflict": mac_conflict_baseline,
    "host_ip_conflict": ip_conflict_baseline,
    "icmp_frag_needed_filter_misconfiguration": frag_needed_baseline,
    "int_insufficient_mtu_headroom": int_headroom_baseline,
    "tcp_syn_flood_attack": syn_flood_baseline,
    "lb_pending_connection_update_race": lb_race_baseline,
    "flow_rule_shadowing": flow_shadow_baseline,
    "p4_ecn_threshold_misconfiguration": ecn_marking_baseline,
    "p4_tcam_entry_corruption": tcam_drop_baseline,
    "silent_egress_packet_loss": silent_loss_baseline,
}


def evaluate_custom_baseline(
    failure: str, problem: Any, params: Any
) -> tuple[bool, dict[str, Any]] | None:
    """Return the healthy pre-inject reading, or ``None`` without a baseline."""
    handler = _CUSTOM_BASELINE.get(failure)
    if handler is None:
        return None
    return handler(problem, params)


def evaluate_custom_symptom(
    failure: str, problem: Any, params: Any
) -> tuple[bool, dict[str, Any]]:
    handler = _CUSTOM.get(failure)
    if handler is None:
        return False, {"error": f"no_custom_handler:{failure}"}
    return handler(problem, params)
