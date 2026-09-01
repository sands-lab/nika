"""Helpers for TCP receive-window limited failure calibration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from nika.net_env.verify import (
    exec_or_empty,
    http_download_stats,
    iperf_throughput_bps,
    median_float,
    ping_stats,
)

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime

SYSCTL_MODERATE = "net.ipv4.tcp_moderate_rcvbuf"
SYSCTL_TCP_RMEM = "net.ipv4.tcp_rmem"
SYSCTL_RMEM_MAX = "net.core.rmem_max"

# Floor/ceiling keep target buffers in the tens-of-KB range.
DEFAULT_BUFFER_FLOOR_BYTES = 16 * 1024
DEFAULT_BUFFER_CEIL_BYTES = 128 * 1024
DEFAULT_BDP_DIVISOR = 8.0
# Reject paths where even the floor cannot sit well below BDP.
MIN_BDP_BYTES = DEFAULT_BUFFER_FLOOR_BYTES * 4


@dataclass(frozen=True)
class SysctlSnapshot:
    moderate_rcvbuf: str
    tcp_rmem: str
    rmem_max: str


@dataclass(frozen=True)
class PathBaseline:
    rtt_ms: float
    throughput_bps: float
    bdp_bytes: float
    target_buffer_bytes: int
    source: str


def sysctl_get(runtime: "LabRuntime", host: str, key: str) -> str:
    return exec_or_empty(runtime, host, f"sysctl -n {key}", timeout=10).strip()


def _proc_path_for_sysctl(key: str) -> str:
    return "/proc/sys/" + key.replace(".", "/")


def sysctl_set(
    runtime: "LabRuntime",
    host: str,
    key: str,
    value: str,
    *,
    required: bool = True,
) -> bool:
    """Write a sysctl via /proc/sys and confirm readback.

    Returns True when the value stuck. When ``required`` is False, permission
    or read-only failures return False instead of raising (Docker often keeps
    ``net.core.rmem_max`` read-only even in privileged containers).
    """
    path = _proc_path_for_sysctl(key)
    out = runtime.exec(
        host,
        f"printf '%s' '{value}' > {path} 2>&1; echo EXIT:$?",
        timeout=10,
    )
    got = sysctl_get(runtime, host, key)
    ok = " ".join(got.split()) == " ".join(value.split())
    if ok:
        return True
    if required:
        raise RuntimeError(
            f"failed to set {key} on {host}: want {value!r}, got {got!r}, "
            f"write_out={out!r}"
        )
    return False


def read_sysctl_snapshot(runtime: "LabRuntime", host: str) -> SysctlSnapshot:
    return SysctlSnapshot(
        moderate_rcvbuf=sysctl_get(runtime, host, SYSCTL_MODERATE),
        tcp_rmem=sysctl_get(runtime, host, SYSCTL_TCP_RMEM),
        rmem_max=sysctl_get(runtime, host, SYSCTL_RMEM_MAX),
    )


def write_sysctl_snapshot(
    runtime: "LabRuntime",
    host: str,
    snap: SysctlSnapshot,
    *,
    require_rmem_max: bool = False,
) -> bool:
    """Apply snapshot. Returns whether rmem_max was successfully updated."""
    sysctl_set(runtime, host, SYSCTL_MODERATE, snap.moderate_rcvbuf)
    sysctl_set(runtime, host, SYSCTL_TCP_RMEM, " ".join(snap.tcp_rmem.split()))
    return sysctl_set(
        runtime,
        host,
        SYSCTL_RMEM_MAX,
        snap.rmem_max.strip(),
        required=require_rmem_max,
    )


def estimate_bdp_bytes(*, throughput_bps: float, rtt_ms: float) -> float:
    if throughput_bps <= 0 or rtt_ms <= 0:
        return 0.0
    return throughput_bps * (rtt_ms / 1000.0) / 8.0


def select_target_buffer_bytes(
    bdp_bytes: float,
    *,
    divisor: float = DEFAULT_BDP_DIVISOR,
    floor: int = DEFAULT_BUFFER_FLOOR_BYTES,
    ceil: int = DEFAULT_BUFFER_CEIL_BYTES,
) -> int:
    if bdp_bytes < MIN_BDP_BYTES:
        raise ValueError(
            f"path BDP {bdp_bytes:.0f} bytes is below minimum {MIN_BDP_BYTES} "
            "for tcp_receive_window_limited; scenario/flow is incompatible"
        )
    raw = int(bdp_bytes / divisor)
    target = max(floor, min(ceil, raw))
    if target >= bdp_bytes * 0.5:
        raise ValueError(
            f"selected receive buffer {target} bytes is not << BDP "
            f"{bdp_bytes:.0f} bytes; scenario/flow is incompatible"
        )
    return target


def primary_ipv4(runtime: "LabRuntime", host: str) -> str | None:
    line = exec_or_empty(
        runtime,
        host,
        "ip -4 -o addr show scope global 2>/dev/null | awk '{print $4}' | head -1",
        timeout=10,
    ).strip()
    if not line:
        return None
    return line.split("/")[0]


def measure_path_baseline(
    runtime: "LabRuntime",
    *,
    receiver: str,
    sender_host: str,
    sender_ip: str,
    large_url: str,
    trials: int = 3,
    iperf_duration_sec: int = 5,
    max_time_sec: int = 180,
    bdp_divisor: float = DEFAULT_BDP_DIVISOR,
    buffer_floor: int = DEFAULT_BUFFER_FLOOR_BYTES,
    buffer_ceil: int = DEFAULT_BUFFER_CEIL_BYTES,
) -> PathBaseline:
    """Median RTT + single-flow TCP throughput, then BDP-derived target buffer.

    Prefers iperf3 for calibration speed; falls back to large HTTP download.
    Bulk direction is sender → receiver so the injected host is the TCP receiver.
    """
    ping = ping_stats(runtime, receiver, sender_ip, count=10, interval_sec=0.2)
    if ping.rtt_avg_ms is None or ping.rtt_avg_ms <= 0:
        raise ValueError(f"unable to measure RTT from {receiver} to {sender_ip}")

    receiver_ip = primary_ipv4(runtime, receiver)
    throughputs: list[float] = []
    source = "iperf3"
    if receiver_ip:
        for _ in range(trials):
            # Sender client pushes to receiver server (receiver is TCP RWND side).
            bps = iperf_throughput_bps(
                runtime,
                sender_host,
                receiver,
                receiver_ip,
                duration_sec=iperf_duration_sec,
            )
            if bps is not None:
                throughputs.append(bps)
    if not throughputs:
        source = "http_large"
        for _ in range(trials):
            stats = http_download_stats(
                runtime, receiver, large_url, max_time_sec=max_time_sec
            )
            if stats.ok and stats.throughput_bps is not None:
                throughputs.append(stats.throughput_bps)

    median_bps = median_float(throughputs)
    if median_bps is None:
        raise ValueError(
            f"unable to measure TCP throughput from {sender_host} toward {receiver}"
        )
    bdp = estimate_bdp_bytes(throughput_bps=median_bps, rtt_ms=ping.rtt_avg_ms)
    target = select_target_buffer_bytes(
        bdp, divisor=bdp_divisor, floor=buffer_floor, ceil=buffer_ceil
    )
    return PathBaseline(
        rtt_ms=ping.rtt_avg_ms,
        throughput_bps=median_bps,
        bdp_bytes=bdp,
        target_buffer_bytes=target,
        source=source,
    )


def format_tcp_rmem(target_bytes: int) -> str:
    """min default max for tcp_rmem; keep min at most 4 KiB."""
    minimum = min(4096, target_bytes)
    return f"{minimum} {target_bytes} {target_bytes}"


def interface_is_up(runtime: "LabRuntime", host: str, intf: str = "eth0") -> bool:
    out = exec_or_empty(
        runtime, host, f"ip -o link show {intf} 2>/dev/null || true", timeout=10
    )
    return "state UP" in out or ",UP" in out or "UP," in out
