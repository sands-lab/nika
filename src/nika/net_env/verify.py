"""Shared helpers for post-deploy net_env verification."""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal, TypeVar

if TYPE_CHECKING:
    from nika.net_env.base import NetworkEnvBase
    from nika.runtime.base import LabRuntime

_T = TypeVar("_T")
_R = TypeVar("_R")


def bounded_parallel_map(
    function: Callable[[_T], _R], items: Iterable[_T], *, max_workers: int = 8
) -> list[_R]:
    """Map independent read-only checks concurrently and preserve input order."""
    values = list(items)
    if len(values) < 2 or max_workers < 2:
        return [function(item) for item in values]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(values))) as pool:
        return list(pool.map(function, values))


def _lab_ready_defaults() -> tuple[float, float]:
    """Return the configured startup-verification window and retry delay."""
    try:
        from nika.run_config.loader import get_run_config

        lab = get_run_config().nika.lab
        return float(lab.ready_max_wait_sec), float(lab.ready_retry_delay_sec)
    except Exception:  # noqa: BLE001
        return 180.0, 5.0


def build_lab_verify_result(
    *,
    scenario_name: str,
    verified: bool,
    checks: dict[str, bool],
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "verified": verified,
        "scenario_name": scenario_name,
        "checks": dict(checks),
        "details": details or {},
    }


def exec_or_empty(
    runtime: "LabRuntime", host: str, command: str, timeout: float = 10.0
) -> str:
    try:
        return runtime.exec(host, command, timeout=timeout)
    except Exception:
        return ""


def nodes_deployed(runtime: "LabRuntime", expected: Iterable[str]) -> bool:
    return set(expected).issubset(set(runtime.list_nodes()))


def ping_ok(runtime: "LabRuntime", host: str, target: str, *, count: int = 1) -> bool:
    output = exec_or_empty(runtime, host, f"ping -c {count} -W 2 {target}", timeout=15)
    return f"{count} received" in output or f"{count} packets received" in output


@dataclass(frozen=True)
class PingStats:
    transmitted: int
    received: int
    loss_percent: float
    rtt_avg_ms: float | None
    rtt_mdev_ms: float | None
    raw: str


def _parse_ping_stats(output: str, *, count: int) -> PingStats:
    transmitted = count
    received = 0
    loss_percent = 100.0
    rtt_avg_ms: float | None = None
    rtt_mdev_ms: float | None = None
    for line in output.splitlines():
        if "packets transmitted" in line:
            parts = line.split(",")
            for part in parts:
                part = part.strip()
                if part.endswith("packets transmitted"):
                    transmitted = int(part.split()[0])
                elif part.endswith("received"):
                    received = int(part.split()[0])
                elif "packet loss" in part:
                    loss_percent = float(part.split("%")[0].strip())
        if "rtt min/avg/max" in line or "round-trip min/avg/max" in line:
            stats = line.split("=")[-1].strip().split()[0]
            fields = stats.split("/")
            if len(fields) >= 2:
                rtt_avg_ms = float(fields[1])
            if len(fields) >= 4:
                rtt_mdev_ms = float(fields[3])
    return PingStats(
        transmitted=transmitted,
        received=received,
        loss_percent=loss_percent,
        rtt_avg_ms=rtt_avg_ms,
        rtt_mdev_ms=rtt_mdev_ms,
        raw=output,
    )


def ping_stats(
    runtime: "LabRuntime",
    host: str,
    target: str,
    *,
    count: int = 20,
    interval_sec: float = 0.2,
    packet_size: int | None = None,
    df: bool = False,
) -> PingStats:
    """Run ping and parse loss and RTT statistics."""
    size_arg = f" -s {packet_size}" if packet_size is not None else ""
    interval_arg = f" -i {interval_sec}" if interval_sec > 0 else ""
    df_arg = " -M do" if df else ""
    timeout = max(15.0, count * (interval_sec + 1.0) + 5.0)
    output = exec_or_empty(
        runtime,
        host,
        f"ping -c {count}{size_arg}{interval_arg}{df_arg} -W 2 {target}",
        timeout=timeout,
    )
    return _parse_ping_stats(output, count=count)


def ping_size_ok(
    runtime: "LabRuntime",
    host: str,
    target: str,
    *,
    packet_size: int,
    count: int = 3,
    df: bool = False,
) -> bool:
    stats = ping_stats(
        runtime, host, target, count=count, packet_size=packet_size, df=df
    )
    return stats.received >= 1


def _frag_needed_in_output(output: str) -> bool:
    lower = output.lower()
    return any(
        token in lower
        for token in (
            "frag needed",
            "fragmentation needed",
            "message too long",
            "frag-needed",
        )
    )


def ping_df_probe(
    runtime: "LabRuntime",
    host: str,
    target: str,
    *,
    packet_size: int,
    count: int = 3,
) -> tuple[bool, bool, str]:
    """Return ``(ok, saw_frag_needed, raw)`` for a DF ping of ``packet_size``."""
    stats = ping_stats(
        runtime, host, target, count=count, packet_size=packet_size, df=True
    )
    ok = stats.received >= 1
    return ok, _frag_needed_in_output(stats.raw), stats.raw


def ping_mtu_frag_needed(
    runtime: "LabRuntime",
    host: str,
    target: str,
    *,
    small_size: int = 64,
    large_size: int = 1400,
) -> bool:
    """True when small DF packets pass and large DF packets fail (path MTU / PMTUD)."""
    small_ok, _, _ = ping_df_probe(runtime, host, target, packet_size=small_size)
    large_ok, _, _ = ping_df_probe(runtime, host, target, packet_size=large_size)
    return small_ok and not large_ok


def ping_mtu_blackhole(
    runtime: "LabRuntime",
    host: str,
    target: str,
    *,
    small_size: int = 64,
    large_size: int = 1400,
) -> bool:
    """True when small DF packets pass and large DF packets fail without Frag Needed."""
    small_ok, _, _ = ping_df_probe(runtime, host, target, packet_size=small_size)
    large_ok, saw_frag, _ = ping_df_probe(runtime, host, target, packet_size=large_size)
    return small_ok and not large_ok and not saw_frag


def dns_resolve_ok(
    runtime: "LabRuntime",
    host: str,
    name: str,
    *,
    expected_ip: str | None = None,
    server: str | None = None,
    timeout_sec: int = 5,
) -> bool:
    server_arg = f" @{server}" if server else ""
    output = exec_or_empty(
        runtime,
        host,
        f"dig +short +time={timeout_sec}{server_arg} {name}",
        timeout=timeout_sec + 10,
    )
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return False
    if expected_ip is None:
        return bool(re.match(r"^[\d.]+$", lines[0]))
    return expected_ip in lines


def http_status(runtime: "LabRuntime", host: str, url: str) -> str:
    return exec_or_empty(
        runtime,
        host,
        f"curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 5 {url}",
        timeout=20,
    ).strip()


def http_time_ms(runtime: "LabRuntime", host: str, url: str) -> float | None:
    output = exec_or_empty(
        runtime,
        host,
        f"curl -s -o /dev/null -w '%{{http_code}} %{{time_total}}' "
        f"--connect-timeout 5 --max-time 20 {url}",
        timeout=25,
    ).strip()
    parts = output.split()
    if len(parts) != 2:
        return None
    code, total = parts[0], parts[1]
    if code not in {"200", "206"}:
        return None
    try:
        return float(total) * 1000.0
    except ValueError:
        return None


def http_body_time_ms(
    runtime: "LabRuntime",
    host: str,
    url: str,
    *,
    max_bytes: int = 8192,
    max_time_sec: int = 15,
) -> float | None:
    """Download up to ``max_bytes`` of the response body and return total time in ms."""
    output = exec_or_empty(
        runtime,
        host,
        f"curl -s -o /dev/null -w '%{{http_code}} %{{time_total}}' --connect-timeout 5 "
        f"--max-time {max_time_sec} --range 0-{max_bytes - 1} {url}",
        timeout=float(max_time_sec + 10),
    ).strip()
    parts = output.split()
    if len(parts) != 2 or parts[0] not in {"200", "206"}:
        return None
    try:
        return float(parts[1]) * 1000.0
    except ValueError:
        return None


@dataclass(frozen=True)
class HttpDownloadStats:
    """One single-connection HTTP GET measurement."""

    ok: bool
    http_code: str
    time_total_s: float | None
    size_bytes: float | None
    throughput_bps: float | None
    raw: str = ""


def http_download_stats(
    runtime: "LabRuntime",
    host: str,
    url: str,
    *,
    max_time_sec: int = 180,
    connect_timeout_sec: int = 10,
    max_bytes: int | None = None,
) -> HttpDownloadStats:
    """Download a URL over a new TCP connection and measure throughput.

    When ``max_bytes`` is set, uses HTTP Range to cap the transfer (faster
    probes that still exercise sustained bulk TCP).
    """
    range_arg = f" --range 0-{max_bytes - 1}" if max_bytes is not None else ""
    output = exec_or_empty(
        runtime,
        host,
        f"curl -s -o /dev/null -w '%{{http_code}} %{{time_total}} %{{size_download}}' "
        f"--connect-timeout {connect_timeout_sec} --max-time {max_time_sec} "
        f"--http1.1{range_arg} {url}",
        timeout=float(max_time_sec + 20),
    ).strip()
    parts = output.split()
    if len(parts) != 3:
        return HttpDownloadStats(
            ok=False,
            http_code="",
            time_total_s=None,
            size_bytes=None,
            throughput_bps=None,
            raw=output,
        )
    code, total_s, size_s = parts
    try:
        time_total = float(total_s)
        size_bytes = float(size_s)
    except ValueError:
        return HttpDownloadStats(
            ok=False,
            http_code=code,
            time_total_s=None,
            size_bytes=None,
            throughput_bps=None,
            raw=output,
        )
    ok = code in {"200", "206"} and time_total > 0 and size_bytes > 0
    bps = (size_bytes * 8.0 / time_total) if ok else None
    return HttpDownloadStats(
        ok=ok,
        http_code=code,
        time_total_s=time_total,
        size_bytes=size_bytes,
        throughput_bps=bps,
        raw=output,
    )


def median_float(values: list[float]) -> float | None:
    """Return the median of ``values``, or None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def median_throughput_bps(
    runtime: "LabRuntime",
    host: str,
    url: str,
    *,
    trials: int = 3,
    max_time_sec: int = 180,
    max_bytes: int | None = None,
) -> float | None:
    """Median single-connection HTTP download throughput over ``trials``."""
    values: list[float] = []
    for _ in range(trials):
        stats = http_download_stats(
            runtime,
            host,
            url,
            max_time_sec=max_time_sec,
            max_bytes=max_bytes,
        )
        if stats.ok and stats.throughput_bps is not None:
            values.append(stats.throughput_bps)
    return median_float(values)


def route_is_onlink(runtime: "LabRuntime", host: str, destination: str) -> bool | None:
    """Return True when ``ip route get`` treats ``destination`` as on-link."""
    output = exec_or_empty(
        runtime, host, f"ip route get {destination} 2>/dev/null || true", timeout=10
    )
    if not output.strip():
        return None
    return " via " not in output.splitlines()[0]


def iperf_throughput_bps(
    runtime: "LabRuntime",
    src_host: str,
    dst_host: str,
    dst_ip: str,
    *,
    duration_sec: int = 3,
    port: int = 5201,
) -> float | None:
    """Run a short iperf3 TCP transfer and return bits/sec (or None on failure)."""
    for host in {src_host, dst_host}:
        runtime.exec(host, "pkill -f 'iperf3' 2>/dev/null || true", timeout=5)
    time.sleep(0.3)
    runtime.exec(
        dst_host,
        f"rm -f /tmp/iperf3_s_{port}.log; "
        f"nohup iperf3 -s -p {port} -1 >/tmp/iperf3_s_{port}.log 2>&1 & echo $!",
        timeout=10,
    )
    time.sleep(0.8)
    # Capture JSON on stdout; file redirects are unreliable through some exec shells.
    raw = exec_or_empty(
        runtime,
        src_host,
        f"iperf3 -c {dst_ip} -p {port} -t {duration_sec} -J 2>/dev/null || true",
        timeout=float(duration_sec + 15),
    )
    runtime.exec(dst_host, "pkill -f 'iperf3' 2>/dev/null || true", timeout=5)
    raw = raw.strip()
    if not raw.startswith("{"):
        # Some wrappers prepend status lines; keep from first JSON object.
        idx = raw.find("{")
        if idx < 0:
            return None
        raw = raw[idx:]
    try:
        import json

        data = json.loads(raw)
        if data.get("error"):
            return None
        end = data.get("end") or {}
        summary = end.get("sum_sent") or end.get("sum_received") or end.get("sum") or {}
        bps = float(summary.get("bits_per_second") or 0.0)
        return bps if bps > 0 else None
    except Exception:  # noqa: BLE001
        return None


def iperf_tcp_metrics(
    runtime: "LabRuntime",
    src_host: str,
    dst_host: str,
    dst_ip: str,
    *,
    duration_sec: int = 3,
    port: int = 5201,
) -> tuple[float | None, int | None]:
    """Run iperf3 and return (bits_per_second, retransmits) or (None, None)."""
    for host in {src_host, dst_host}:
        runtime.exec(host, "pkill -f 'iperf3' 2>/dev/null || true", timeout=5)
    time.sleep(0.3)
    runtime.exec(
        dst_host,
        f"rm -f /tmp/iperf3_s_{port}.log; "
        f"nohup iperf3 -s -p {port} -1 >/tmp/iperf3_s_{port}.log 2>&1 & echo $!",
        timeout=10,
    )
    time.sleep(0.8)
    raw = exec_or_empty(
        runtime,
        src_host,
        f"iperf3 -c {dst_ip} -p {port} -t {duration_sec} -J 2>/dev/null || true",
        timeout=float(duration_sec + 15),
    )
    runtime.exec(dst_host, "pkill -f 'iperf3' 2>/dev/null || true", timeout=5)
    raw = raw.strip()
    if not raw.startswith("{"):
        idx = raw.find("{")
        if idx < 0:
            return None, None
        raw = raw[idx:]
    try:
        import json

        data = json.loads(raw)
        if data.get("error"):
            return None, None
        end = data.get("end") or {}
        summary = end.get("sum_sent") or end.get("sum_received") or end.get("sum") or {}
        bps = float(summary.get("bits_per_second") or 0.0)
        retrans = summary.get("retransmits")
        retrans_int = int(retrans) if retrans is not None else None
        return (bps if bps > 0 else None, retrans_int)
    except Exception:  # noqa: BLE001
        return None, None


def tbf_overlimits(runtime: "LabRuntime", host: str, intf: str = "eth0") -> int | None:
    """Return TBF overlimits/drops counter from ``tc -s qdisc``, if present."""
    output = exec_or_empty(
        runtime, host, f"tc -s qdisc show dev {intf} 2>/dev/null || true", timeout=10
    )
    if "tbf" not in output:
        return None
    total = 0
    found = False
    for line in output.splitlines():
        lower = line.lower()
        if "overlimits" in lower or "dropped" in lower:
            found = True
            for token in line.replace(",", " ").split():
                if token.isdigit():
                    total += int(token)
    return total if found else 0


SymptomExpectation = Literal[
    "reachable",
    "unreachable",
    "loss_increased",
    "latency_increased",
    "degraded",
    "gray_loss",
    "control_plane_down",
    "isolation",
    "none",
]


def compare_symptom(
    before: dict[str, Any],
    after: dict[str, Any],
    expect: SymptomExpectation,
    *,
    loss_min_percent: float = 10.0,
    latency_factor: float = 2.0,
) -> tuple[bool, dict[str, Any]]:
    """Compare baseline vs post-inject probe snapshots for behavioral tests."""
    details: dict[str, Any] = {"expect": expect, "before": before, "after": after}
    if expect == "none":
        return True, details
    if expect == "reachable":
        ok = bool(after.get("ping_ok") or after.get("http_ok"))
        details["observed"] = ok
        return ok, details
    if expect == "unreachable":
        ping_before = before.get("ping_ok")
        ping_after = after.get("ping_ok")
        http_before = before.get("http_ok")
        http_after = after.get("http_ok")
        mtu_blackhole = after.get("mtu_blackhole")
        mtu_frag_needed = after.get("mtu_frag_needed")
        ping_broke = ping_before and not ping_after
        http_broke = http_before and not http_after
        ok = (
            ping_broke
            or http_broke
            or (not ping_after and not http_after)
            or bool(mtu_blackhole)
            or bool(mtu_frag_needed)
        )
        details["observed"] = {
            "ping_broke": ping_broke,
            "http_broke": http_broke,
            "mtu_blackhole": mtu_blackhole,
            "mtu_frag_needed": mtu_frag_needed,
        }
        return ok, details
    if expect == "loss_increased":
        before_loss = float(before.get("loss_percent") or 0.0)
        after_loss = float(after.get("loss_percent") or 0.0)
        ok = after_loss >= loss_min_percent and after_loss > before_loss
        details["observed"] = {
            "before_loss": before_loss,
            "after_loss": after_loss,
        }
        return ok, details
    if expect == "latency_increased":
        before_ms = before.get("rtt_avg_ms")
        after_ms = after.get("rtt_avg_ms")
        if before_ms is None or after_ms is None:
            before_ms = before.get("http_time_ms")
            after_ms = after.get("http_time_ms")
        if before_ms is None or after_ms is None:
            # Absolute latency gate for DNS delay faults when no baseline snap.
            ok = after_ms is not None and float(after_ms) >= 500.0
        else:
            ok = float(after_ms) >= float(before_ms) * latency_factor
        details["observed"] = {"before_ms": before_ms, "after_ms": after_ms}
        return ok, details
    if expect == "gray_loss":
        after_loss = float(after.get("loss_percent") or 0.0)
        ok = 0.5 <= after_loss <= 15.0
        details["observed"] = {"after_loss": after_loss}
        return ok, details
    if expect == "control_plane_down":
        ok = not bool(after.get("control_plane_ok", True))
        details["observed"] = after.get("control_plane_ok")
        return ok, details
    if expect == "isolation":
        after_symptom = after.get("symptom_ok")
        after_control = after.get("control_ok")
        symptom_broken = after_symptom is False
        control_intact = True if after_control is None else bool(after_control)
        ok = bool(symptom_broken) and control_intact
        details["observed"] = {
            "symptom_broken": symptom_broken,
            "control_intact": control_intact,
        }
        return ok, details
    if expect == "degraded":
        before_ms = before.get("http_time_ms")
        after_ms = after.get("http_time_ms")
        http_broke = before.get("http_ok") and not after.get("http_ok")
        slower_http = (
            before_ms is not None
            and after_ms is not None
            and float(after_ms) > float(before_ms) * latency_factor
        )
        before_rtt = before.get("rtt_avg_ms")
        after_rtt = after.get("rtt_avg_ms")
        slower_rtt = (
            before_rtt is not None
            and after_rtt is not None
            and float(after_rtt) >= float(before_rtt) * latency_factor
        )
        # Absolute latency is evidence only when the healthy baseline was below
        # the threshold. A pre-existing slow service must degrade further.
        absolute_slow = (
            before_ms is not None
            and float(before_ms) < 500.0
            and after_ms is not None
            and float(after_ms) >= 500.0
        )
        before_bps = before.get("bits_per_second")
        after_bps = after.get("bits_per_second")
        throughput_drop = (
            after_bps is not None
            and float(after_bps) > 0
            and (
                (before_bps is not None and float(after_bps) < float(before_bps) * 0.5)
                or float(after_bps) < 100_000.0
            )
        )
        onlink = after.get("route_onlink")
        ok = (
            http_broke
            or slower_http
            or slower_rtt
            or absolute_slow
            or throughput_drop
            or bool(onlink)
        )
        details["observed"] = {
            "http_broke": http_broke,
            "slower_http": slower_http,
            "slower_rtt": slower_rtt,
            "absolute_slow": absolute_slow,
            "throughput_drop": throughput_drop,
            "route_onlink": onlink,
            "before_ms": before_ms,
            "after_ms": after_ms,
            "before_rtt": before_rtt,
            "after_rtt": after_rtt,
            "before_bps": before_bps,
            "after_bps": after_bps,
        }
        return ok, details
    return False, details


def link_up(runtime: "LabRuntime", host: str, intf: str = "eth0") -> bool:
    return (
        exec_or_empty(runtime, host, f"cat /sys/class/net/{intf}/operstate").strip()
        == "up"
    )


def host_has_ipv4(
    runtime: "LabRuntime", host: str, address: str, intf: str = "eth0"
) -> bool:
    output = exec_or_empty(runtime, host, f"ip -4 -o addr show dev {intf}")
    return address in output


def default_route_via(runtime: "LabRuntime", host: str, gateway: str) -> bool:
    return f"via {gateway}" in exec_or_empty(runtime, host, "ip route show default")


def process_running(runtime: "LabRuntime", host: str, process: str) -> bool:
    return bool(exec_or_empty(runtime, host, f"pgrep -x {process}").strip())


def service_active(runtime: "LabRuntime", host: str, unit: str) -> bool:
    return (
        exec_or_empty(runtime, host, f"systemctl is-active {unit}").strip() == "active"
    )


def http_ok(runtime: "LabRuntime", host: str, url: str) -> bool:
    output = exec_or_empty(
        runtime,
        host,
        f"curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 5 {url}",
        timeout=20,
    )
    return output.strip() == "200"


def frr_bgp_established(
    runtime: "LabRuntime", router: str, *, min_neighbors: int = 1
) -> bool:
    output = exec_or_empty(runtime, router, "vtysh -c 'show bgp summary'", timeout=20)
    if not output.strip() or "failed to connect" in output.lower():
        return False
    established = 0
    for line in output.splitlines():
        if "Established" in line:
            established += 1
        elif line.split() and line.split()[-1].isdigit() and int(line.split()[-1]) > 0:
            # Prefix count column when the state column is omitted in summaries.
            established += 1
    return established >= min_neighbors


def frr_bgp_has_established_session(runtime: "LabRuntime", router: str) -> bool:
    """Return whether any BGP neighbor is in Established state."""
    return frr_bgp_established(runtime, router, min_neighbors=1)


def k8s_ready_node_count(output: str) -> int:
    ready = 0
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "Ready":
            ready += 1
    return ready


def k8s_namespace_phase_active(output: str) -> bool:
    """True when ``kubectl get ns … -o jsonpath={.status.phase}`` is Active.

    Kathara ``exec`` returns stderr text without raising on NotFound, so a
    substring check like ``\"llm-d\" in output`` falsely matches error messages.
    """
    return output.strip() == "Active"


def _k8s_lab_nodes_running(net_env: NetworkEnvBase) -> tuple[bool, list[str]]:
    """Return whether all k3s lab nodes are running and any dead node names."""
    nodes = list(getattr(net_env, "kubernetes_nodes", []) or [])
    if not nodes:
        return True, []
    dead: list[str] = []
    try:
        runtime = net_env._build_runtime()
        for node in nodes:
            try:
                if runtime.get_container(node).status != "running":
                    dead.append(node)
            except Exception:
                dead.append(node)
    except Exception:
        return False, nodes
    return not dead, dead


def _restart_dead_k8s_nodes(net_env: NetworkEnvBase, dead_nodes: list[str]) -> None:
    """Best-effort Docker start for k3s node containers that exited mid-boot."""
    if not dead_nodes:
        return
    try:
        runtime = net_env._build_runtime()
    except Exception:
        return
    for node in dead_nodes:
        try:
            container = runtime.get_container(node)
            if container.status != "running":
                container.start()
        except Exception:
            continue


def verify_lab_with_retry(net_env: NetworkEnvBase) -> dict[str, Any] | None:
    """Poll ``net_env.verify_lab()`` until success or timeout.

    Returns ``None`` when the scenario defines no startup verification.
    """
    verify = getattr(net_env, "startup_verify_lab", net_env.verify_lab)
    result = verify()
    if result is None:
        return None

    default_wait, default_delay = _lab_ready_defaults()
    max_wait_sec = getattr(net_env, "VERIFY_MAX_WAIT_SEC", default_wait)
    retry_delay_sec = getattr(net_env, "VERIFY_RETRY_DELAY_SEC", default_delay)
    deadline = time.time() + max_wait_sec
    last_result = result
    dead_since: float | None = None
    restarted = False
    while time.time() < deadline:
        nodes_ok, dead_nodes = _k8s_lab_nodes_running(net_env)
        if not nodes_ok:
            now = time.time()
            if dead_since is None:
                dead_since = now
            # After ~60s of dead nodes, try one Docker restart wave.
            if not restarted and now - dead_since >= 60:
                _restart_dead_k8s_nodes(net_env, dead_nodes)
                restarted = True
                dead_since = now
            # Abort if nodes stay down for another 3 minutes after restart (or 4 min total).
            abort_after = 180.0 if restarted else 240.0
            if now - dead_since >= abort_after or now + retry_delay_sec >= deadline:
                raise RuntimeError(
                    f"Lab verification aborted for {net_env.name!r}: "
                    f"k3s node container(s) not running: {dead_nodes or ['unknown']}"
                )
            time.sleep(retry_delay_sec)
            continue
        dead_since = None
        last_result = verify()
        if last_result.get("verified", False):
            return last_result
        time.sleep(retry_delay_sec)

    failed_checks = {
        name: ok for name, ok in (last_result.get("checks") or {}).items() if not ok
    }
    raise RuntimeError(
        f"Lab verification failed for {net_env.name!r} "
        f"within {max_wait_sec}s; failed checks: {failed_checks or last_result}"
    )
