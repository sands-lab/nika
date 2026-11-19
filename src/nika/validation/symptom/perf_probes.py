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
    """Measure transfer performance before constraining either endpoint."""
    if params.host_name == params.client_host:
        return _receiver_cpu_baseline(problem, params)
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
    """Bulk transfers slow down while the peer confirms path health."""
    if params.host_name == params.client_host:
        return _receiver_cpu_contention(problem, params)
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
_STREAM_RUNS = 3
# Share of the sender's busy time spent waiting on the receiver's advertised
# window (Linux tcp_info ``tcpi_rwnd_limited / tcpi_busy_time``). A receiver
# that drains its socket keeps the window open whatever the path or host load
# does to the rate; a starved one lets it fill. Per stream, measured 0.35-0.84
# under the dc_clos and llmd faults (lowest on a heavily loaded host, where
# the sender slows too) and 0.00-0.02 healthy, also on a 10 Mbit or 20 ms path.
_RECEIVER_MIN_RWND_SHARE = 0.2
_HEALTHY_RWND_SHARE_MAX = 0.1
# Offset of tcpi_busy_time, tcpi_rwnd_limited, tcpi_sndbuf_limited (u64 us)
# in struct tcp_info (include/uapi/linux/tcp.h, Linux >= 4.10).
_TCP_INFO_LIMITS_OFFSET = 168


def _stream(runtime: Any, sender: str, receiver_ip: str) -> dict[str, Any]:
    """Send up to ``_STREAM_BYTES`` for at most ``_STREAM_SEC``.

    Reports the rate and how much of the transfer the receiver's window held
    the sender back, read from the sender's own socket.
    """
    src = (
        "import socket,struct,time\n"
        f"s=socket.create_connection(({receiver_ip!r},{_SINK_PORT}),timeout=5)\n"
        "s.settimeout(1.0); buf=b'x'*65536; sent=0; t0=time.monotonic()\n"
        f"while sent<{_STREAM_BYTES} and time.monotonic()-t0<{_STREAM_SEC}:\n"
        "  try: sent+=s.send(buf)\n"
        "  except socket.timeout: pass\n"
        "dt=time.monotonic()-t0\n"
        "i=s.getsockopt(socket.IPPROTO_TCP,socket.TCP_INFO,256)\n"
        f"busy,rwnd,sndbuf=struct.unpack_from('QQQ',i,{_TCP_INFO_LIMITS_OFFSET})\n"
        "s.close(); print('SENT',sent,dt,busy,rwnd,sndbuf)\n"
    )
    out = runtime.exec(
        sender, f"python3 -c {shlex.quote(src)} 2>&1", timeout=_STREAM_SEC + 15
    )
    parts = out.split()
    if "SENT" not in parts or len(parts) < parts.index("SENT") + 6:
        return {"ok": False, "raw": out[-300:]}
    start = parts.index("SENT") + 1
    sent, seconds, busy_us, rwnd_us, sndbuf_us = (
        float(value) for value in parts[start : start + 5]
    )
    return {
        "ok": True,
        "bytes": int(sent),
        "seconds": round(seconds, 2),
        "bps": sent * 8.0 / seconds if seconds > 0 else None,
        "rwnd_limited_share": round(rwnd_us / busy_us, 3) if busy_us else None,
        "sndbuf_limited_share": round(sndbuf_us / busy_us, 3) if busy_us else None,
    }


def _start_sink(runtime: Any, host: str) -> None:
    """(Re)start the draining sink on ``host`` and wait until it listens."""
    # Two calls: `pkill -f` in the same shell line as the sink's command line
    # matches that shell and kills it before the sink starts.
    runtime.exec(host, f"pkill -f '[s]ink-{_SINK_PORT}' 2>/dev/null")
    runtime.exec(
        host,
        f"nohup python3 -c {shlex.quote(_SINK_SRC)} sink-{_SINK_PORT} "
        "</dev/null >/dev/null 2>&1 & "
        # Wait until it listens, so the first stream is not refused.
        f"for _ in $(seq 50); do ss -Hltn 'sport = :{_SINK_PORT}' | grep -q . "
        "&& break; sleep 0.2; done",
        timeout=20,
    )


def _median_rwnd_share(streams: list[dict[str, Any]]) -> float | None:
    shares = [s.get("rwnd_limited_share") for s in streams]
    if any(share is None for share in shares):
        return None
    return median_float(shares)


def _receiver_cpu_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    runtime = problem.runtime
    receiver_ip = runtime.get_host_ip(params.host_name, with_prefix=False)
    if not params.server_host or not receiver_ip:
        return False, {"error": "no_sender_peer_or_receiver_ip"}
    _start_sink(runtime, params.host_name)
    streams = [
        _stream(runtime, params.server_host, receiver_ip) for _ in range(_STREAM_RUNS)
    ]
    ping = ping_stats(
        runtime, params.server_host, receiver_ip, count=40, interval_sec=0.2
    )
    sample = {
        "sender": params.server_host,
        "receiver_ip": receiver_ip,
        "streams": streams,
        "median_bps": median_float([s["bps"] for s in streams if s.get("bps")]),
        "rwnd_limited_share": _median_rwnd_share(streams),
        "ping_loss_percent": ping.loss_percent,
    }
    problem._audit_receiver_baseline = sample
    healthy = bool(
        all(s["ok"] for s in streams)
        and sample["median_bps"]
        and sample["rwnd_limited_share"] is not None
        and sample["rwnd_limited_share"] <= _HEALTHY_RWND_SHARE_MAX
        and ping.loss_percent is not None
        and ping.loss_percent <= _MAX_LOSS_PERCENT
    )
    return healthy, sample


def _receiver_cpu_contention(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Observe socket-drain starvation entirely from the healthy peer."""
    base = getattr(problem, "_audit_receiver_baseline", None)
    if not base or not base.get("median_bps"):
        return False, {"error": "no_healthy_baseline"}
    streams = [
        _stream(problem.runtime, base["sender"], base["receiver_ip"])
        for _ in range(_STREAM_RUNS)
    ]
    if not all(s["ok"] for s in streams):
        return False, {"error": "stream_failed", "streams": streams}
    bps = median_float([s["bps"] for s in streams])
    ratio = bps / base["median_bps"] if bps is not None else None
    rwnd_share = _median_rwnd_share(streams)
    ping = ping_stats(
        problem.runtime, base["sender"], base["receiver_ip"], count=40, interval_sec=0.2
    )
    control_ok = (
        ping.loss_percent is not None and ping.loss_percent <= _MAX_LOSS_PERCENT
    )
    verified = bool(
        rwnd_share is not None and rwnd_share >= _RECEIVER_MIN_RWND_SHARE and control_ok
    )
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "host": params.host_name,
            "baseline": base,
            "streams": streams,
            "injected_throughput_bps": bps,
            "throughput_ratio": ratio,
            "rwnd_limited_share": rwnd_share,
            "ping_loss_percent": ping.loss_percent,
            "ping_rtt_ms": ping.rtt_avg_ms,
            "control_ok": control_ok,
        },
    )


# ------------------------------------------------ TCP receive window

_IPERF_RUNS = 3


def _iperf_median(
    runtime: Any,
    sender: str,
    receiver: str,
    ip: str,
    *,
    timeout_sec: float | None = None,
) -> dict:
    runs = [
        iperf_throughput_bps(
            runtime, sender, receiver, ip, duration_sec=3, timeout_sec=timeout_sec
        )
        for _ in range(_IPERF_RUNS)
    ]
    measured = [bps for bps in runs if bps]
    return {
        "runs_bps": runs,
        "median_bps": median_float(measured) if len(measured) == len(runs) else None,
    }


def _rwnd_control_hosts(problem: Any, params: Any) -> list[str]:
    """Other CORP hosts, those at the receiver's site first, so they share the path.

    Hosts that the scenario's business traffic downloads into are skipped:
    their own transfers come and go, so iperf3 readings to them vary.
    """
    from nika.traffic.workload import BULK_DOWNLOAD

    site = params.host_name.split("_", 1)[0]
    workload = problem.net_env.business_workload()
    busy = {t[0] for t in workload.targets(BULK_DOWNLOAD)} if workload else set()
    hosts = [
        h
        for h in problem.runtime.list_nodes() or []
        if "_pc" in h and h not in {params.host_name, params.sender_host, *busy}
    ]
    return sorted(
        hosts, key=lambda h: ("corp" not in h, not h.startswith(f"{site}_"), h)
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
    """Bulk TCP into the receiver is window-bound; another receiver is not.

    Throughput against the healthy iperf3 reading is reported, not judged.
    """
    base = getattr(problem, "_audit_rwnd_baseline", None)
    if not base:
        return False, {"error": "no_healthy_baseline"}
    runtime = problem.runtime
    # A restricted receive window and a congested path delay connection setup
    # and the final control exchange beyond the timed transfer itself.
    fault_timeout_sec = 180.0
    path = _iperf_median(
        runtime,
        params.sender_host,
        params.host_name,
        base["receiver_ip"],
        timeout_sec=fault_timeout_sec,
    )
    control = _iperf_median(
        runtime,
        params.sender_host,
        base["control"],
        base["control_ip"],
        timeout_sec=fault_timeout_sec,
    )
    small = http_download_stats(
        runtime, params.host_name, params.small_url, max_time_sec=30
    )
    # The decision reads the probe stream's own sender-side tcp_info: the
    # share of its busy time spent waiting on the receiver's window does not
    # move with host load or the receiver's own business traffic, as the
    # throughput ratio against a healthy iperf3 median does. The sink starts
    # after inject so its socket takes the receive buffer the fault set.
    _start_sink(runtime, params.host_name)
    streams = [
        _stream(runtime, params.sender_host, base["receiver_ip"])
        for _ in range(_STREAM_RUNS)
    ]
    streams_ok = all(s["ok"] for s in streams)
    rwnd_share = _median_rwnd_share(streams) if streams_ok else None

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
        rwnd_share is not None and rwnd_share >= _RECEIVER_MIN_RWND_SHARE and control_ok
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
            "streams": streams,
            "rwnd_limited_share": rwnd_share,
            "small_http_ok": small.ok,
            "control_ok": control_ok,
        },
    )
