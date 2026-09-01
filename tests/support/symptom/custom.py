"""Custom evaluate_symptom handlers for failures with domain-specific gates."""

from __future__ import annotations

import time
from typing import Any

from nika.net_env.verify import http_download_stats, ping_stats
from nika.problems.base import build_verify_result
from nika.problems.service_networking.ab_helpers import ab_summary_to_dict
from tests.support.symptom.flap_probes import evaluate_link_flap_symptom
from tests.support.symptom.corruption_probes import (
    evaluate_device_forwarding_corruption_symptom,
    evaluate_link_capacity_symptom,
    evaluate_link_corruption_symptom,
)
from nika.problems.service_networking.load_balancer import (
    _BACKEND_CPU_MAX_RATIO,
    _BACKEND_LOCAL_URL,
    _CONTROL_ABS_P95_MAX_MS,
    _CONTROL_P95_BASELINE_MAX_RATIO,
    _CONTROL_VS_VIP_MAX_RATIO,
    _MAX_LOSS_PERCENT as _LB_MAX_LOSS_PERCENT,
    _MIN_ERROR_COUNT_FOR_DEGRADATION,
    _NGINX_CPU_MIN_RATIO,
    _RTT_ABS_MAX_MS,
    _RTT_MAX_RATIO as _LB_RTT_MAX_RATIO,
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
        quota_cpus=params.cpu_quota,
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
        and (
            (
                control_p95_ratio is not None
                and control_p95_ratio <= _CONTROL_P95_BASELINE_MAX_RATIO
            )
            or (
                control.p95_ms <= _CONTROL_ABS_P95_MAX_MS
                and vip.p95_ms is not None
                and control.p95_ms <= vip.p95_ms * _CONTROL_VS_VIP_MAX_RATIO
            )
        )
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
    if ping.rtt_avg_ms is not None:
        rtt_ok = ping.rtt_avg_ms <= _RTT_ABS_MAX_MS
        if base_rtt is not None and base_rtt > 0:
            rtt_ok = rtt_ok or ping.rtt_avg_ms <= base_rtt * _LB_RTT_MAX_RATIO
        path_ok = path_ok and rtt_ok

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
    injected_bps, injected_time_s = problem._median_large_stats(
        params, url, max_time_sec=300, trials=3
    )
    baseline_bps = problem._baseline_throughput_bps
    baseline_time = problem._baseline_time_s
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


_CUSTOM: dict[str, Any] = {
    "link_flap": evaluate_link_flap_symptom,
    "link_packet_corruption": evaluate_link_corruption_symptom,
    "link_capacity_bottleneck": evaluate_link_capacity_symptom,
    "device_forwarding_packet_corruption": evaluate_device_forwarding_corruption_symptom,
    "web_dos_attack": _web_dos,
    "sender_resource_contention": _sender_resource_contention,
    "receiver_resource_contention": _receiver_resource_contention,
    "load_balancer_overload": _load_balancer_overload,
    "lb_connection_state_exhaustion": _lb_connection_state_exhaustion,
}


def evaluate_custom_symptom(
    failure: str, problem: Any, params: Any
) -> tuple[bool, dict[str, Any]]:
    handler = _CUSTOM.get(failure)
    if handler is None:
        return False, {"error": f"no_custom_handler:{failure}"}
    return handler(problem, params)
