"""Shared helpers for post-deploy net_env verification."""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from ipaddress import IPv4Address
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from nika.net_env.base import NetworkEnvBase
    from nika.runtime.base import LabRuntime


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
    exit_code: int | None = None


def http_download_stats(
    runtime: "LabRuntime",
    host: str,
    url: str,
    *,
    max_time_sec: int = 180,
    connect_timeout_sec: int = 10,
    max_bytes: int | None = None,
    output_path: str = "/dev/null",
    compressed: bool = False,
) -> HttpDownloadStats:
    """Download a URL over a new TCP connection and measure throughput.

    When ``max_bytes`` is set, uses HTTP Range to cap the transfer (faster
    probes that still exercise sustained bulk TCP).
    """
    range_arg = f" --range 0-{max_bytes - 1}" if max_bytes is not None else ""
    encoding_arg = " --compressed" if compressed else ""
    output = exec_or_empty(
        runtime,
        host,
        f"curl -s -o {shlex.quote(output_path)} "
        "-w '%{http_code} %{time_total} %{size_download}' "
        f"--connect-timeout {connect_timeout_sec} --max-time {max_time_sec} "
        f"--http1.1{range_arg}{encoding_arg} {shlex.quote(url)}; "
        "result=$?; printf ' %s' \"$result\"",
        timeout=float(max_time_sec + 20),
    ).strip()
    parts = output.split()
    if len(parts) != 4:
        return HttpDownloadStats(
            ok=False,
            http_code="",
            time_total_s=None,
            size_bytes=None,
            throughput_bps=None,
            raw=output,
        )
    code, total_s, size_s, exit_s = parts
    try:
        time_total = float(total_s)
        size_bytes = float(size_s)
        exit_code = int(exit_s)
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
        exit_code=exit_code,
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


def _start_iperf3_server(runtime: "LabRuntime", host: str, port: int) -> str | None:
    """Start a one-shot iperf3 server and wait until it listens; return its PID."""
    output = runtime.exec(
        host,
        f"rm -f /tmp/iperf3_s_{port}.log; "
        f"nohup iperf3 -s -p {port} -1 >/tmp/iperf3_s_{port}.log 2>&1 & echo $!",
        timeout=10,
    )
    tokens = output.split()
    pid = tokens[-1] if tokens and tokens[-1].isdigit() else None
    probe = (
        f"ss -Hltn 'sport = :{port}' 2>/dev/null || "
        f"netstat -ltn 2>/dev/null | grep ':{port} ' || true"
    )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if f":{port}" in exec_or_empty(runtime, host, probe, timeout=5):
            break
        time.sleep(0.1)
    return pid


def _stop_iperf3_server(runtime: "LabRuntime", host: str, pid: str | None) -> None:
    # Kill only the server started here; scenario background iperf3 stays up.
    if pid:
        runtime.exec(host, f"kill {pid} 2>/dev/null || true", timeout=5)


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
    bps, _ = iperf_tcp_metrics(
        runtime, src_host, dst_host, dst_ip, duration_sec=duration_sec, port=port
    )
    return bps


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
    pid = _start_iperf3_server(runtime, dst_host, port)
    try:
        # Capture JSON on stdout; file redirects are unreliable through some
        # exec shells.
        raw = exec_or_empty(
            runtime,
            src_host,
            f"iperf3 -c {dst_ip} -p {port} -t {duration_sec} -J 2>/dev/null || true",
            timeout=float(duration_sec + 15),
        )
    finally:
        _stop_iperf3_server(runtime, dst_host, pid)
    raw = raw.strip()
    if not raw.startswith("{"):
        # Some wrappers prepend status lines; keep from first JSON object.
        idx = raw.find("{")
        if idx < 0:
            return None, None
        raw = raw[idx:]
    try:
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
        before_loss_raw = before.get("loss_percent")
        after_loss_raw = after.get("loss_percent")
        loss_degraded = False
        if after_loss_raw is not None:
            after_loss = float(after_loss_raw)
            before_loss = float(before_loss_raw) if before_loss_raw is not None else 0.0
            loss_degraded = after_loss >= loss_min_percent and after_loss > before_loss
        onlink = after.get("route_onlink")
        ok = (
            http_broke
            or slower_http
            or slower_rtt
            or absolute_slow
            or throughput_drop
            or loss_degraded
            or bool(onlink)
        )
        details["observed"] = {
            "http_broke": http_broke,
            "slower_http": slower_http,
            "slower_rtt": slower_rtt,
            "absolute_slow": absolute_slow,
            "throughput_drop": throughput_drop,
            "loss_degraded": loss_degraded,
            "route_onlink": onlink,
            "before_ms": before_ms,
            "after_ms": after_ms,
            "before_rtt": before_rtt,
            "after_rtt": after_rtt,
            "before_bps": before_bps,
            "after_bps": after_bps,
            "before_loss": before_loss_raw,
            "after_loss": after_loss_raw,
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


def service_active(
    runtime: "LabRuntime", host: str, unit: str, *, timeout: float = 10.0
) -> bool:
    return (
        exec_or_empty(
            runtime, host, f"systemctl is-active {unit}", timeout=timeout
        ).strip()
        == "active"
    )


# Re-runs frrinit.sh start, which only spawns watchfrr; running daemons keep
# their state and sessions (same command the FRR startup scripts use).
_FRR_HEAL_COMMAND = "service frr start"


def should_heal_frr(
    *, unit_active: bool, zebra_running: bool, already_healed: bool
) -> bool:
    """Decide whether to re-spawn watchfrr on a node whose frr unit is down.

    watchfrr exits for good when none of its daemons answer within its fixed
    55 s startup window (slow boot under host load). The daemons finish
    starting anyway, so routing works while ``systemctl is-active frr`` stays
    ``failed``. Heal only that state, and only once per node.
    """
    return not unit_active and zebra_running and not already_healed


def frr_active_or_heal(
    runtime: "LabRuntime",
    host: str,
    healed: set[str] | None = None,
    *,
    timeout: float = 20.0,
) -> bool:
    """Return whether FRR is usable for startup, healing a dead watchfrr once.

    ``healed`` records nodes already healed during this lab's startup
    verification. Pass ``None`` to check without healing.

    Under host load watchfrr can exit while zebra/BGP keep running and the
    ``frr`` systemd unit stays ``failed``. After one heal attempt, treat a
    live zebra process as success so startup verify matches routing reality.
    """
    if service_active(runtime, host, "frr", timeout=timeout):
        return True
    zebra_running = bool(
        exec_or_empty(runtime, host, "pgrep -x zebra", timeout=timeout).strip()
    )
    if healed is None:
        return False
    if should_heal_frr(
        unit_active=False,
        zebra_running=zebra_running,
        already_healed=host in healed,
    ):
        healed.add(host)
        from nika.utils.logger import log_warning_event

        log_warning_event(
            "env_verify_frr_heal",
            f"frr unit not active on {host} while zebra is running "
            f"(watchfrr exited); running '{_FRR_HEAL_COMMAND}' once",
            host=host,
            command=_FRR_HEAL_COMMAND,
        )
        exec_or_empty(runtime, host, _FRR_HEAL_COMMAND, timeout=60.0)
        if service_active(runtime, host, "frr", timeout=timeout):
            return True
        zebra_running = bool(
            exec_or_empty(runtime, host, "pgrep -x zebra", timeout=timeout).strip()
        )
    return zebra_running


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


def frr_bgp_established_peers(summary: str) -> set[str]:
    """Parse FRR ``show bgp summary`` neighbor lines for Established peers.

    FRR columns: Neighbor V AS MsgRcvd MsgSent TblVer InQ OutQ Up/Down
    State/PfxRcd PfxSnt [Desc]. Established peers show a numeric PfxRcd;
    other sessions show their FSM state (Idle, Active, Connect, ...).
    Numbered and unnumbered (interface or hostname) neighbors both count.
    """
    peers: set[str] = set()
    for line in summary.splitlines():
        fields = line.split()
        if len(fields) < 10 or fields[1] not in {"4", "6"}:
            continue
        if not fields[2].isdigit():
            continue
        state = fields[9]
        if state.isdigit() or state == "Established":
            peers.add(fields[0])
    return peers


def srl_bgp_established_peers(output: str) -> set[str]:
    """Parse SR Linux ``show network-instance ... bgp neighbor`` for Established peers.

    Only table rows count: the trailing summary ("0 configured sessions are
    established") also contains the word.
    """
    peers: set[str] = set()
    for line in output.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) > 6 and cells[6] == "established":
            peers.add(cells[2])
    return peers


def frr_ospf_full_router_ids(output: str) -> set[str]:
    """Parse FRR ``show ip ospf neighbor`` for router IDs in Full state."""
    peers: set[str] = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 3 and any(field.startswith("Full") for field in fields):
            try:
                peers.add(str(IPv4Address(fields[0])))
            except ValueError:
                continue
    return peers


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


def raise_for_k8s_startup_failure(
    runtime: LabRuntime, containers: Mapping[str, Any]
) -> None:
    """Surface controller bootstrap errors or exited k3s nodes immediately."""
    for node, container in containers.items():
        container.reload()
        if container.status != "running":
            raise RuntimeError(
                f"k3s node {node!r} exited during startup: "
                f"{container.attrs.get('State', {})}; "
                f"{container.logs(tail=30).decode(errors='replace')[-4000:]}"
            )
    failure = exec_or_empty(
        runtime, "controller", "cat /var/run/nika-startup-failed 2>/dev/null || true"
    ).strip()
    # A busy controller can time out the read; only the marker itself is a failure.
    if failure and not failure.startswith("[TIMEOUT]"):
        log = exec_or_empty(runtime, "controller", "tail -60 /var/log/startup.log")
        raise RuntimeError(f"k3s controller bootstrap failed: {failure}\n{log}")


def _runtime_validation_depth() -> Literal["light", "full"]:
    """Return configured post-deploy verification depth."""
    try:
        from nika.run_config.loader import get_run_config

        return get_run_config().nika.runtime_validation.depth
    except Exception:  # noqa: BLE001
        return "light"


class LabVerifyTimeoutError(RuntimeError):
    """Lab readiness polling exhausted without a verified result."""

    def __init__(
        self,
        message: str,
        *,
        last_result: dict[str, Any] | None = None,
        failed_checks: dict[str, bool] | None = None,
        max_wait_sec: float | None = None,
    ) -> None:
        super().__init__(message)
        self.last_result = last_result or {}
        self.failed_checks = failed_checks or {}
        self.max_wait_sec = max_wait_sec


def verify_lab_with_retry(net_env: NetworkEnvBase) -> dict[str, Any] | None:
    """Poll startup or full lab verification until success or timeout.

    With ``nika.runtime_validation.depth: light`` (default), prefer
    ``startup_verify_lab`` when present. With ``full``, always poll
    ``verify_lab``.

    Returns ``None`` when the scenario defines no startup verification.
    """
    from nika.utils.logger import log_event
    from nika.utils.session_log_summaries import failed_checks_map, summarize_lab_verify

    if _runtime_validation_depth() == "full":
        verify = net_env.verify_lab
    else:
        verify = getattr(net_env, "startup_verify_lab", net_env.verify_lab)
    k3s_nodes = list(getattr(net_env, "kubernetes_nodes", []) or [])
    k3s_runtime = net_env._build_runtime() if k3s_nodes else None
    k3s_containers = {node: k3s_runtime.get_container(node) for node in k3s_nodes}
    if k3s_nodes:
        raise_for_k8s_startup_failure(k3s_runtime, k3s_containers)
    result = verify()
    if result is None:
        return None

    default_wait, default_delay = _lab_ready_defaults()
    max_wait_sec = getattr(net_env, "VERIFY_MAX_WAIT_SEC", default_wait)
    retry_delay_sec = getattr(net_env, "VERIFY_RETRY_DELAY_SEC", default_delay)
    started = time.time()
    deadline = started + max_wait_sec
    last_result = result
    # Log immediately, then about every 30s (or each retry if slower).
    progress_interval_sec = max(30.0, float(retry_delay_sec))
    last_progress_log = 0.0

    def _emit_verify_progress(*, force: bool = False) -> None:
        nonlocal last_progress_log
        now = time.time()
        if not force and (now - last_progress_log) < progress_interval_sec:
            return
        last_progress_log = now
        elapsed = now - started
        summary = summarize_lab_verify(last_result)
        message = (
            f"Lab verification pending for {net_env.name} "
            f"({elapsed:.0f}s / {max_wait_sec:.0f}s): {summary}"
        )
        log_event(
            "env_verify_progress",
            message,
            lab_name=net_env.name,
            elapsed_sec=round(elapsed, 1),
            max_wait_sec=max_wait_sec,
            checks=last_result.get("checks"),
            details=last_result.get("details") or {},
            failed_checks=failed_checks_map(last_result.get("checks")),
        )

    if not result.get("verified", False):
        _emit_verify_progress(force=True)

    while time.time() < deadline:
        if k3s_nodes:
            raise_for_k8s_startup_failure(k3s_runtime, k3s_containers)
        last_result = verify()
        if k3s_nodes:
            runtime = k3s_runtime
            raise_for_k8s_startup_failure(runtime, k3s_containers)
            complete = (
                exec_or_empty(
                    runtime,
                    "controller",
                    "test -f /var/run/nika-startup-complete && echo complete",
                ).strip()
                == "complete"
            )
            last_result["checks"]["bootstrap_complete"] = complete
            last_result["verified"] = bool(last_result.get("verified") and complete)
        if last_result.get("verified", False):
            return last_result
        _emit_verify_progress()
        time.sleep(retry_delay_sec)

    failed_checks = failed_checks_map(last_result.get("checks"))
    raise LabVerifyTimeoutError(
        f"Lab verification failed for {net_env.name!r} "
        f"within {max_wait_sec}s; failed checks: {failed_checks or last_result}",
        last_result=last_result,
        failed_checks=failed_checks,
        max_wait_sec=max_wait_sec,
    )
