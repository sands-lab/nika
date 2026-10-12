"""Endpoint and service performance symptom probes (test-path only).

Each fault here degrades one endpoint (a web server under attack, a CPU-starved
sender or receiver, a receiver with a tiny TCP window). The baseline handler
takes the same measurement on the healthy lab and keeps it on the problem; the
symptom handler compares against it, so the sham read on the healthy lab sees
no change. Every probe also measures a control that shares the network path
but not the faulty endpoint, so the slowdown is localized to the root cause.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from nika.net_env.verify import (
    http_download_stats,
    iperf_throughput_bps,
    median_float,
    ping_stats,
)
from nika.problems.base import build_verify_result
from nika.problems.endpoint_application.transport import (
    _THROUGHPUT_MAX_RATIO as SENDER_MAX_RATIO,
)

# A service counts as degraded when its median latency grows this much.
_SLOWDOWN_FACTOR = 5.0
_SLOWDOWN_MIN_MS = 25.0
_ERROR_RATE_MIN = 0.4
_HEALTHY_ERROR_RATE_MAX = 0.2
_THROUGHPUT_MAX_RATIO = 0.5
_CONTROL_MIN_RATIO = 0.5
_MAX_LOSS_PERCENT = 5.0


def _http_samples(
    runtime: Any, host: str, url: str, *, samples: int, timeout_sec: int
) -> dict[str, Any]:
    times_ms: list[float] = []
    codes: list[str] = []
    for _ in range(samples):
        stats = http_download_stats(
            runtime,
            host,
            url,
            max_time_sec=timeout_sec,
            connect_timeout_sec=min(3, timeout_sec),
        )
        codes.append(stats.http_code)
        if stats.ok and stats.time_total_s is not None:
            times_ms.append(stats.time_total_s * 1000.0)
    return {
        "observer": host,
        "url": url,
        "attempts": samples,
        "successes": len(times_ms),
        "error_rate": 1.0 - len(times_ms) / samples,
        "median_ms": median_float(times_ms),
        "samples_ms": times_ms,
        "http_codes": codes,
    }


def _slower(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Median latency or error rate clearly worse than the healthy reading."""
    if before.get("error_rate", 1.0) > _HEALTHY_ERROR_RATE_MAX:
        return False
    if after["error_rate"] >= _ERROR_RATE_MIN:
        return True
    old, new = before.get("median_ms"), after.get("median_ms")
    return (
        old is not None
        and new is not None
        and new >= old * _SLOWDOWN_FACTOR
        and new - old >= _SLOWDOWN_MIN_MS
    )


# ---------------------------------------------------------------- web DoS


def _router_hops(runtime: Any, src: str, ip: str) -> int | None:
    """Routers between ``src`` and ``ip``, from the echo reply TTL (Linux sends 64)."""
    match = re.search(r"ttl=(\d+)", runtime.exec(src, f"ping -c 1 -W 2 {ip}"))
    return 64 - int(match.group(1)) if match else None


def _web_dos_ends(problem: Any, params: Any) -> dict[str, Any]:
    """Victim URL, and the other web server nearest to the observer.

    The flood's responses load every link between the victim and the attacker.
    The nearest other server shares the fewest links with them (in dc_clos it
    sits in the observer's pod, off the super-spine links the flood crosses),
    so the control isolates the victim from shared-link congestion.
    """
    runtime = problem.runtime
    observer = params.observer_device or params.attacker_device
    victim_ip = runtime.get_host_ip(params.host_name, with_prefix=False)
    busy = {params.host_name, params.attacker_device, params.observer_device}
    web = list((getattr(problem.net_env, "servers", None) or {}).get("web") or [])
    candidates = []
    for host in sorted(h for h in web if h not in busy):
        ip = runtime.get_host_ip(host, with_prefix=False)
        hops = _router_hops(runtime, observer, ip) if ip else None
        if hops is not None:
            candidates.append((hops, host, ip))
    control = min(candidates) if candidates else None
    return {
        "observer": observer,
        "url": problem._probe_url(params, victim_ip),
        "control_url": f"http://{control[2]}/" if control else None,
        "control_hops": control[0] if control else None,
    }


def _web_dos_sample(problem: Any, params: Any, ends: dict[str, Any]) -> dict:
    sample = {
        name: _http_samples(
            problem.runtime,
            ends["observer"],
            url,
            samples=params.probe_samples,
            timeout_sec=params.probe_timeout_sec,
        )
        for name, url in (("victim", ends["url"]), ("control", ends["control_url"]))
        if url
    }
    return sample


def web_dos_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    ends = _web_dos_ends(problem, params)
    problem._audit_web_ends = ends
    sample = _web_dos_sample(problem, params, ends)
    problem._audit_web_baseline = sample
    healthy = all(
        part["error_rate"] <= _HEALTHY_ERROR_RATE_MAX for part in sample.values()
    )
    return healthy and "control" in sample, {**sample, "ends": ends}


def web_dos(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The victim answers far slower or not at all; another server does not."""
    ends = getattr(problem, "_audit_web_ends", None) or _web_dos_ends(problem, params)
    base = getattr(problem, "_audit_web_baseline", None) or {}
    victim_before = base.get("victim") or problem._baseline
    if victim_before is None:
        return False, {"error": "no_healthy_baseline"}
    after = _web_dos_sample(problem, params, ends)
    degraded = _slower(victim_before, after["victim"])
    control_before = base.get("control")
    control_ok = bool(
        control_before
        and "control" in after
        and after["control"]["error_rate"] <= _HEALTHY_ERROR_RATE_MAX
        and not _slower(control_before, after["control"])
    )
    verified = degraded and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "ends": ends,
            "baseline": base or {"victim": victim_before},
            "after": after,
            "degraded": degraded,
            "control_ok": control_ok,
        },
    )


# ------------------------------------------------- sender CPU contention


def sender_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Healthy bulk download from the sender's CPU-bound file server."""
    problem._ensure_http_objects(params)
    ping = ping_stats(
        problem.runtime, params.client_host, params.dst_ip, count=10, interval_sec=0.2
    )
    bps, seconds = problem._median_large_stats(params, max_time_sec=300)
    small = http_download_stats(
        problem.runtime, params.client_host, params.small_url, max_time_sec=30
    )
    problem._baseline_throughput_bps = bps
    problem._baseline_time_s = seconds
    problem._baseline_rtt_ms = ping.rtt_avg_ms
    healthy = bool(
        bps
        and small.ok
        and ping.loss_percent is not None
        and ping.loss_percent <= _MAX_LOSS_PERCENT
    )
    return healthy, {
        "throughput_bps": bps,
        "time_s": seconds,
        "small_http_ok": small.ok,
        "ping_rtt_ms": ping.rtt_avg_ms,
        "ping_loss_percent": ping.loss_percent,
    }


def sender_contention(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Bulk downloads from the sender slow down; small objects and the path do not."""
    baseline_bps = problem._baseline_throughput_bps
    baseline_time = problem._baseline_time_s
    if not baseline_bps or not baseline_time:
        return False, {"error": "no_healthy_baseline"}
    ping = ping_stats(
        problem.runtime, params.client_host, params.dst_ip, count=10, interval_sec=0.2
    )
    small = http_download_stats(
        problem.runtime, params.client_host, params.small_url, max_time_sec=30
    )
    bps, seconds, probe_timeout_sec = problem.measure_fault_degradation(params)
    ratio = bps / baseline_bps if bps is not None else None
    # The network path and the server's small objects stay healthy.
    control_ok = bool(
        ping.loss_percent is not None
        and ping.loss_percent <= _MAX_LOSS_PERCENT
        and small.ok
    )
    verified = bool(ratio is not None and ratio <= SENDER_MAX_RATIO and control_ok)
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "host": params.host_name,
            "client_host": params.client_host,
            "baseline_throughput_bps": baseline_bps,
            "baseline_time_s": baseline_time,
            "baseline_rtt_ms": problem._baseline_rtt_ms,
            "injected_throughput_bps": bps,
            "injected_time_s": seconds,
            "throughput_ratio": ratio,
            "probe_timeout_sec": probe_timeout_sec,
            "small_http_ok": small.ok,
            "ping_rtt_ms": ping.rtt_avg_ms,
            "ping_loss_percent": ping.loss_percent,
            "control_ok": control_ok,
        },
    )


# ----------------------------------------------- receiver CPU contention

# The receiving application: it reads a TCP stream and discards it. It runs on
# the receiver before inject, so the starved host needs no command afterwards;
# the peer measures how fast the receiver drains the stream.
_SINK_PORT = 19620
_SINK_SRC = (
    "import socket\n"
    "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)\n"
    f"s.bind(('0.0.0.0',{_SINK_PORT})); s.listen(4)\n"
    "while True:\n"
    "  c,_=s.accept()\n"
    "  while c.recv(65536): pass\n"
    "  c.close()\n"
)
_STREAM_BYTES = 64 * 1024 * 1024
_STREAM_SEC = 15
# Starved-receiver rates vary run to run; a median of three is stable.
_STREAM_RUNS = 3


def _stream(runtime: Any, sender: str, receiver_ip: str) -> dict[str, Any]:
    """Send up to ``_STREAM_BYTES`` for at most ``_STREAM_SEC``; report the rate."""
    src = (
        "import socket,time\n"
        f"s=socket.create_connection(({receiver_ip!r},{_SINK_PORT}),timeout=5)\n"
        "s.settimeout(1.0); buf=b'x'*65536; sent=0; t0=time.monotonic()\n"
        f"while sent<{_STREAM_BYTES} and time.monotonic()-t0<{_STREAM_SEC}:\n"
        "  try: sent+=s.send(buf)\n"
        "  except socket.timeout: pass\n"
        "dt=time.monotonic()-t0; s.close(); print('SENT',sent,dt)\n"
    )
    out = runtime.exec(
        sender, f"python3 -c {shlex.quote(src)} 2>&1", timeout=_STREAM_SEC + 15
    )
    parts = out.split()
    if "SENT" not in parts or len(parts) < parts.index("SENT") + 3:
        return {"ok": False, "raw": out[-300:]}
    sent = int(parts[parts.index("SENT") + 1])
    seconds = float(parts[parts.index("SENT") + 2])
    return {
        "ok": True,
        "bytes": sent,
        "seconds": round(seconds, 2),
        "bps": sent * 8.0 / seconds if seconds > 0 else None,
    }


def receiver_baseline(problem: Any, params: Any) -> tuple[bool, dict] | None:
    if problem.scenario_name == "campus_lan":
        bps, seconds = problem._median_large_stats(
            params, problem._resolve_large_url(params), max_time_sec=120
        )
        return bool(bps and seconds), {"downloads": problem._last_downloads}
    runtime = problem.runtime
    peers = problem._resolve_peer_hosts(params)
    receiver_ip = runtime.get_host_ip(params.host_name, with_prefix=False)
    if not peers or not receiver_ip:
        return False, {"error": "no_sender_peer_or_receiver_ip"}
    # Two calls: `pkill -f` in the same shell line as the sink's command line
    # matches that shell and kills it before the sink starts.
    runtime.exec(params.host_name, f"pkill -f '[s]ink-{_SINK_PORT}' 2>/dev/null")
    runtime.exec(
        params.host_name,
        f"nohup python3 -c {shlex.quote(_SINK_SRC)} sink-{_SINK_PORT} "
        "</dev/null >/dev/null 2>&1 & "
        # Wait until it listens, so the first stream is not refused.
        f"for _ in $(seq 50); do ss -Hltn 'sport = :{_SINK_PORT}' | grep -q . "
        "&& break; sleep 0.2; done",
        timeout=20,
    )
    streams = [_stream(runtime, peers[0], receiver_ip) for _ in range(_STREAM_RUNS)]
    rates = [s["bps"] for s in streams if s.get("bps")]
    ping = ping_stats(runtime, peers[0], receiver_ip, count=10, interval_sec=0.2)
    sample = {
        "sender": peers[0],
        "receiver_ip": receiver_ip,
        "streams": streams,
        "median_bps": median_float(rates),
        "ping_loss_percent": ping.loss_percent,
        "ping_rtt_ms": ping.rtt_avg_ms,
    }
    problem._audit_receiver_baseline = sample
    healthy = bool(
        len(rates) == len(streams) and ping.loss_percent == 0.0 and sample["median_bps"]
    )
    return healthy, sample


def _receiver_campus(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    url = problem._resolve_large_url(params)
    bps, seconds = problem._median_large_stats(params, url, max_time_sec=120)
    samples = list(problem._last_downloads)
    baseline = problem._baseline_throughput_bps
    ratio = bps / baseline if bps and baseline else None
    control_bps, _ = problem._median_large_stats(
        params, url, max_time_sec=120, host=problem._control_host(params)
    )
    control_base = problem._control_baseline_bps
    control_ratio = control_bps / control_base if control_bps and control_base else None
    # The download is CPU-bound by design, so shared-host load slows the
    # control PC too; only a slowdown twice the control's is receiver-local.
    control_ok = (
        ratio is not None and control_ratio is not None and ratio <= 0.5 * control_ratio
    )
    ok = ratio is not None and ratio <= 0.50 and control_ok
    return ok, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=ok,
        details={
            "host": params.host_name,
            "large_url": url,
            "baseline_throughput_bps": baseline,
            "injected_throughput_bps": bps,
            "injected_time_s": seconds,
            "throughput_ratio": ratio,
            "downloads": samples,
            "control_host": problem._control_host(params),
            "control_throughput_ratio": control_ratio,
            "control_ok": control_ok,
        },
    )


def receiver_contention(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The receiver drains a TCP stream far slower; its network path is healthy."""
    if problem.scenario_name == "campus_lan":
        return _receiver_campus(problem, params)
    base = getattr(problem, "_audit_receiver_baseline", None)
    if not base or not base.get("median_bps"):
        return False, {"error": "no_healthy_baseline"}
    runtime = problem.runtime
    streams = [
        _stream(runtime, base["sender"], base["receiver_ip"])
        for _ in range(_STREAM_RUNS)
    ]
    if not all(s["ok"] for s in streams):
        return False, {"error": "stream_failed", "streams": streams}
    bps = median_float([s["bps"] or 0.0 for s in streams])
    ratio = bps / base["median_bps"] if bps is not None else None
    ping = ping_stats(
        runtime, base["sender"], base["receiver_ip"], count=10, interval_sec=0.2
    )
    control_ok = (
        ping.loss_percent is not None and ping.loss_percent <= _MAX_LOSS_PERCENT
    )
    verified = bool(ratio is not None and ratio <= _THROUGHPUT_MAX_RATIO and control_ok)
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "host": params.host_name,
            "baseline": base,
            "streams": streams,
            "median_bps": bps,
            "throughput_ratio": ratio,
            "ping_loss_percent": ping.loss_percent,
            "ping_rtt_ms": ping.rtt_avg_ms,
            "control_ok": control_ok,
        },
    )


# ------------------------------------------------ TCP receive window

_IPERF_RUNS = 3


def _iperf_median(runtime: Any, sender: str, receiver: str, ip: str) -> dict:
    runs = [
        iperf_throughput_bps(runtime, sender, receiver, ip, duration_sec=3)
        for _ in range(_IPERF_RUNS)
    ]
    measured = [bps for bps in runs if bps]
    return {
        "runs_bps": runs,
        "median_bps": median_float(measured) if len(measured) == len(runs) else None,
    }


def _rwnd_control_hosts(problem: Any, params: Any) -> list[str]:
    """Other end hosts, those at the receiver's site first, so they share the path."""
    site = params.host_name.split("_", 1)[0]
    hosts = [
        h
        for h in problem.runtime.list_nodes() or []
        if "_pc" in h and h not in {params.host_name, params.sender_host}
    ]
    return sorted(
        hosts, key=lambda h: (not h.startswith(f"{site}_"), "corp" not in h, h)
    )


def rwnd_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    from nika.problems.traffic_queueing_resource.tcp_rwnd_helpers import primary_ipv4

    runtime = problem.runtime
    receiver_ip = primary_ipv4(runtime, params.host_name)
    if not receiver_ip:
        return False, {"error": "no_receiver_ip"}
    path = _iperf_median(runtime, params.sender_host, params.host_name, receiver_ip)
    # The first host the sender can stream to is the control receiver.
    tried: list[str] = []
    control = control_ip = None
    control_path: dict[str, Any] = {"median_bps": None}
    for candidate in _rwnd_control_hosts(problem, params)[:4]:
        tried.append(candidate)
        candidate_ip = primary_ipv4(runtime, candidate)
        if not candidate_ip:
            continue
        control_path = _iperf_median(
            runtime, params.sender_host, candidate, candidate_ip
        )
        if control_path["median_bps"]:
            control, control_ip = candidate, candidate_ip
            break
    sample = {
        "receiver": params.host_name,
        "receiver_ip": receiver_ip,
        "control": control,
        "control_ip": control_ip,
        "control_candidates_tried": tried,
        "path": path,
        "control_path": control_path,
    }
    problem._audit_rwnd_baseline = sample
    healthy = bool(
        sample["path"]["median_bps"] and sample["control_path"]["median_bps"]
    )
    return healthy, sample


def rwnd_limited(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Bulk TCP into the receiver is window-bound; another receiver is not."""
    base = getattr(problem, "_audit_rwnd_baseline", None)
    if not base:
        return False, {"error": "no_healthy_baseline"}
    runtime = problem.runtime
    path = _iperf_median(
        runtime, params.sender_host, params.host_name, base["receiver_ip"]
    )
    control = _iperf_median(
        runtime, params.sender_host, base["control"], base["control_ip"]
    )
    small = http_download_stats(
        runtime, params.host_name, params.small_url, max_time_sec=30
    )

    def ratio(now: dict, before: dict) -> float | None:
        if now["median_bps"] is None or not before["median_bps"]:
            return None
        return now["median_bps"] / before["median_bps"]

    path_ratio = ratio(path, base["path"])
    control_ratio = ratio(control, base["control_path"])
    control_ok = bool(
        small.ok and control_ratio is not None and control_ratio >= _CONTROL_MIN_RATIO
    )
    verified = bool(
        path_ratio is not None and path_ratio < _THROUGHPUT_MAX_RATIO and control_ok
    )
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "baseline": base,
            "path": path,
            "control_path": control,
            "throughput_ratio": path_ratio,
            "control_throughput_ratio": control_ratio,
            "small_http_ok": small.ok,
            "control_ok": control_ok,
        },
    )


# -------------------------------------------------- load balancer overload

_LB_SAMPLES = 10
_LB_TIMEOUT_SEC = 10


def _lb_sample(problem: Any, params: Any) -> dict[str, Any]:
    return {
        name: _http_samples(
            problem.runtime,
            params.client_host,
            url,
            samples=_LB_SAMPLES,
            timeout_sec=_LB_TIMEOUT_SEC,
        )
        for name, url in (("vip", params.vip_url), ("control", params.control_url))
    }


def lb_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The VIP and the parallel web server both answer before the surge."""
    sample = _lb_sample(problem, params)
    problem._audit_lb_baseline = sample
    healthy = all(
        part["error_rate"] <= _HEALTHY_ERROR_RATE_MAX for part in sample.values()
    )
    return healthy, sample


def lb_overload(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Requests through the VIP slow down or fail; the parallel server does not."""
    base = getattr(problem, "_audit_lb_baseline", None)
    if not base:
        return False, {"error": "no_healthy_baseline"}
    after = _lb_sample(problem, params)
    degraded = _slower(base["vip"], after["vip"])
    control_ok = after["control"][
        "error_rate"
    ] <= _HEALTHY_ERROR_RATE_MAX and not _slower(base["control"], after["control"])
    lb_cpu = problem._cpu_ratio_of_quota(
        params.host_name,
        quota_cpus=problem._applied_quota(params),
        sample_sec=params.cpu_sample_sec,
    )
    verified = degraded and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "baseline": base,
            "after": after,
            "degraded": degraded,
            "lb_cpu_ratio_of_quota": lb_cpu,
            "applied_cpu_quota": problem._applied_quota(params),
            "control_ok": control_ok,
        },
    )
