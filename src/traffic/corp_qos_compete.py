"""Ephemeral CORP QoS competition workload (EF realtime + CS0 bulk).

Reusable by QoS-related failures. Does not reside in lab startup; callers
start/measure/stop around inject and verify.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from nika.net_env.enterprise_branch.topology import (
    DSCP_CS0,
    DSCP_EF,
    TOS_CS0,
    TOS_EF,
    TopoSize,
    corp_pairs_sharing_overlay_egress,
    overlay_qos_for,
)
from nika.runtime.base import LabRuntime
from nika.utils.logger import system_logger

# Voice-like background EF; measure probe fills most of the EF reservation so
# demotion into the saturated BE FIFO is unambiguous.
_EF_BG_RATE_KBIT = 200
_EF_MEASURE_RATE_KBIT = 1200
_EF_PKT_BYTES = 160
# Drive BE well above its hard ceil so demoted EF sees drops and queue delay.
_BULK_LOAD_FRAC = 3.5
_DEFAULT_MEASURE_SEC = 5
_BULK_PORT_BASE = 5201
_EF_PORT = 5199
_PROBE_PORT = 5198


@dataclass
class FlowSpec:
    src_host: str
    dst_host: str
    dst_ip: str
    rate_kbit: int
    tos: int
    port: int
    udp: bool = True
    length: int = 1472
    role: str = "bulk"  # "ef" | "bulk"


@dataclass
class CompeteMatrix:
    """One EF foreground flow plus CS0 bulks on the same overlay egress."""

    edge: str
    intf_name: str
    topo_size: TopoSize
    ef: FlowSpec
    bulks: list[FlowSpec] = field(default_factory=list)


@dataclass
class CompeteHandle:
    runtime: LabRuntime
    matrix: CompeteMatrix
    labels: list[str] = field(default_factory=list)
    started_at: float = 0.0


@dataclass
class FlowMetrics:
    jitter_ms: float | None = None
    lost_percent: float | None = None
    latency_ms: float | None = None
    latency_mdev_ms: float | None = None
    bits_per_second: float | None = None
    packets: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def host_ip(runtime: LabRuntime, host: str) -> str:
    ip = runtime.get_data_plane_host_ip(host, with_prefix=False)
    if not ip:
        ip = runtime.get_host_ip(host, "eth0", with_prefix=False)
    return str(ip).split("/")[0]


def _host_ip(runtime: LabRuntime, host: str) -> str:
    return host_ip(runtime, host)


def build_compete_matrix(
    runtime: LabRuntime,
    *,
    topo_size: TopoSize,
    edge: str,
    intf_name: str,
    src_host: str,
    dst_host: str,
) -> CompeteMatrix:
    """Build an egress-scoped EF + CS0 bulk matrix for ``edge``/``intf_name``."""
    qos = overlay_qos_for(topo_size)
    dst_ip = _host_ip(runtime, dst_host)
    ef = FlowSpec(
        src_host=src_host,
        dst_host=dst_host,
        dst_ip=dst_ip,
        rate_kbit=_EF_BG_RATE_KBIT,
        tos=TOS_EF,
        port=_EF_PORT,
        length=_EF_PKT_BYTES,
        role="ef",
    )

    be_mbit = max(qos.rate_mbit - qos.ef_mbit, 1)
    aggregate_kbit = int(be_mbit * 1000 * _BULK_LOAD_FRAC)
    pairs = corp_pairs_sharing_overlay_egress(
        topo_size,
        edge=edge,
        intf_name=intf_name,
        exclude=(src_host, dst_host),
    )
    if not pairs:
        pairs = [(src_host, dst_host)]
    n = min(qos.bulk_flow_count, max(len(pairs), 1))
    selected: list[tuple[str, str]] = []
    idx = 0
    while len(selected) < n:
        selected.append(pairs[idx % len(pairs)])
        idx += 1

    per_flow = max(aggregate_kbit // n, 1000)
    bulks: list[FlowSpec] = []
    for i, (b_src, b_dst) in enumerate(selected):
        bulks.append(
            FlowSpec(
                src_host=b_src,
                dst_host=b_dst,
                dst_ip=_host_ip(runtime, b_dst),
                rate_kbit=per_flow,
                tos=TOS_CS0,
                port=_BULK_PORT_BASE + i,
                length=1200,
                role="bulk",
            )
        )
    return CompeteMatrix(
        edge=edge,
        intf_name=intf_name,
        topo_size=topo_size,
        ef=ef,
        bulks=bulks,
    )


def _kill_port_listeners(runtime: LabRuntime, host: str, port: int) -> None:
    """Kill TCP listeners on ``port`` without pkill -f self-matching the exec."""
    runtime.exec(
        host,
        "python3 - <<'PY'\n"
        "import os, re, signal, subprocess\n"
        f"port = {port}\n"
        "try:\n"
        "    out = subprocess.check_output(['ss', '-tlnp'], text=True,\n"
        "                                 stderr=subprocess.DEVNULL)\n"
        "except Exception:\n"
        "    raise SystemExit(0)\n"
        "needle = f':{port}'\n"
        "pids = set()\n"
        "for line in out.splitlines():\n"
        "    if needle not in line:\n"
        "        continue\n"
        "    # Require exact port token (avoid :520 matching :5201).\n"
        "    if not re.search(rf'{needle}(?!\\d)', line):\n"
        "        continue\n"
        "    for m in re.finditer(r'pid=(\\d+)', line):\n"
        "        pids.add(int(m.group(1)))\n"
        "for pid in pids:\n"
        "    try:\n"
        "        os.kill(pid, signal.SIGTERM)\n"
        "    except ProcessLookupError:\n"
        "        pass\n"
        "PY",
    )


def _wait_iperf_listen(
    runtime: LabRuntime, host: str, port: int, *, timeout: float = 8.0
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = runtime.exec(host, "ss -tlnp 2>/dev/null || true")
        if re.search(rf":{port}(?!\d)", out):
            return True
        time.sleep(0.25)
    return False


def _start_iperf_server(runtime: LabRuntime, host: str, port: int) -> None:
    _kill_port_listeners(runtime, host, port)
    # Launcher script keeps the Kathara exec cmdline free of `iperf3 -s -p …`.
    runtime.exec(
        host,
        f"printf '%s\\n' '#!/bin/sh' 'exec iperf3 -s -p {port}' "
        f"> /tmp/iperf_s_{port}.sh && chmod +x /tmp/iperf_s_{port}.sh && "
        f"rm -f /tmp/iperf3_s_{port}.log && "
        f"nohup /tmp/iperf_s_{port}.sh >/tmp/iperf3_s_{port}.log 2>&1 &",
    )
    if not _wait_iperf_listen(runtime, host, port):
        log = runtime.exec(host, f"cat /tmp/iperf3_s_{port}.log 2>/dev/null || true")
        system_logger.warning(
            f"iperf3 server on {host}:{port} did not listen in time; log={log!r}"
        )


def _start_iperf_client(runtime: LabRuntime, flow: FlowSpec, duration: int) -> None:
    runtime.exec(
        flow.src_host,
        f"printf '%s\\n' '#!/bin/sh' "
        f"'exec iperf3 -c {flow.dst_ip} -p {flow.port} -u "
        f"-b {flow.rate_kbit}K -t {duration} -l {flow.length} "
        f"-S {flow.tos:#x} -i 1' > /tmp/iperf_c_{flow.port}.sh && "
        f"chmod +x /tmp/iperf_c_{flow.port}.sh && "
        f"nohup /tmp/iperf_c_{flow.port}.sh >/tmp/iperf3_c_{flow.port}.log 2>&1 &",
    )


def start(
    runtime: LabRuntime, matrix: CompeteMatrix, *, duration_sec: int = 300
) -> CompeteHandle:
    """Start EF + bulk flows; keep them running for ``duration_sec``."""
    labels: list[str] = []
    servers: set[tuple[str, int]] = set()
    for flow in [matrix.ef, *matrix.bulks]:
        key = (flow.dst_host, flow.port)
        if key not in servers:
            _start_iperf_server(runtime, flow.dst_host, flow.port)
            servers.add(key)
        _start_iperf_client(runtime, flow, duration_sec)
        labels.append(f"{flow.role}:{flow.src_host}->{flow.dst_host}:{flow.port}")
    time.sleep(2.0)
    system_logger.info(
        f"Started CORP QoS compete workload on {matrix.edge}/{matrix.intf_name}: "
        f"{len(labels)} flows"
    )
    return CompeteHandle(
        runtime=runtime, matrix=matrix, labels=labels, started_at=time.monotonic()
    )


def _parse_iperf_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.rfind("{")
        if start >= 0:
            try:
                return json.loads(text[start:])
            except json.JSONDecodeError:
                return {"raw": text}
        return {"raw": text}


def _parse_ping_rtt(ping_out: str) -> tuple[float | None, float | None]:
    for line in ping_out.splitlines():
        if "rtt min/avg/max" in line or "round-trip" in line:
            parts = line.split("=")[-1].strip().split()[0].split("/")
            if len(parts) >= 4:
                return float(parts[1]), float(parts[3])
            if len(parts) >= 2:
                return float(parts[1]), None
    return None, None


def measure(
    handle: CompeteHandle,
    *,
    duration_sec: int = _DEFAULT_MEASURE_SEC,
) -> FlowMetrics:
    """Measure EF performance under the active compete workload."""
    runtime = handle.runtime
    ef = handle.matrix.ef
    port = ef.port + 50
    _start_iperf_server(runtime, ef.dst_host, port)
    out_path = f"/tmp/iperf3_measure_{port}.json"
    # Bulk fills the BE standing queue; EF-mark the iperf TCP control path so
    # healthy (non-demoted) measurement still completes under load.
    runtime.exec(
        ef.src_host,
        f"iptables -t mangle -C OUTPUT -p tcp --dport {port} "
        f"-j DSCP --set-dscp {DSCP_EF} 2>/dev/null || "
        f"iptables -t mangle -A OUTPUT -p tcp --dport {port} "
        f"-j DSCP --set-dscp {DSCP_EF} || true",
    )
    # runtime.exec defaults to 10s; iperf duration alone can exceed that.
    iperf_timeout = float(duration_sec + 20)
    runtime.exec(
        ef.src_host,
        f"iperf3 -c {ef.dst_ip} -p {port} -u -b {_EF_MEASURE_RATE_KBIT}K "
        f"-t {duration_sec} -l {ef.length} -S {ef.tos:#x} -J > {out_path} 2>"
        f"/tmp/iperf3_measure_{port}.err || true",
        timeout=iperf_timeout,
    )
    runtime.exec(
        ef.src_host,
        f"iptables -t mangle -D OUTPUT -p tcp --dport {port} "
        f"-j DSCP --set-dscp {DSCP_EF} 2>/dev/null || true",
    )
    raw = runtime.exec(ef.src_host, f"cat {out_path} 2>/dev/null || true")
    err = runtime.exec(
        ef.src_host, f"cat /tmp/iperf3_measure_{port}.err 2>/dev/null || true"
    )
    data = _parse_iperf_json(raw)
    metrics = FlowMetrics(raw=data)
    if data.get("error"):
        metrics.error = str(data["error"])
    elif not data and err.strip():
        metrics.error = err.strip()[:200]
    elif raw.strip().startswith("[TIMEOUT]"):
        metrics.error = raw.strip()[:200]
    try:
        end = data.get("end") or {}
        summary = end.get("sum_received") or end.get("sum") or {}
        if summary:
            metrics.jitter_ms = float(summary.get("jitter_ms") or 0.0)
            metrics.lost_percent = float(summary.get("lost_percent") or 0.0)
            metrics.packets = int(summary.get("packets") or 0)
            metrics.bits_per_second = float(summary.get("bits_per_second") or 0.0)
        # Dense ping so latency reflects BE standing-queue delay after demotion.
        ping_out = runtime.exec(
            ef.src_host,
            f"ping -c 30 -i 0.05 -W 2 -Q {ef.tos} {ef.dst_ip} 2>/dev/null || "
            f"ping -c 30 -i 0.05 -W 2 {ef.dst_ip} 2>/dev/null || true",
            timeout=25.0,
        )
        avg, mdev = _parse_ping_rtt(ping_out)
        metrics.latency_ms = avg
        metrics.latency_mdev_ms = mdev
        metrics.raw["ping"] = ping_out[-500:]
    except Exception as exc:  # noqa: BLE001
        metrics.error = str(exc)
    return metrics


def sample_dscp_tos(
    runtime: LabRuntime,
    *,
    capture_host: str,
    capture_iface: str,
    src_host: str,
    dst_ip: str,
    send_tos: int = TOS_EF,
    port: int = _PROBE_PORT,
) -> int | None:
    """Capture UDP TOS via tcpdump after sending packets with ``send_tos``."""
    runtime.exec(
        capture_host,
        "killall -q tcpdump 2>/dev/null || true; "
        "rm -f /tmp/nika_dscp_tos.out /tmp/nika_dscp_tos.err /tmp/nika_dscp_cap.sh",
    )
    runtime.exec(
        capture_host,
        "cat > /tmp/nika_dscp_cap.sh <<'EOF'\n"
        "#!/bin/sh\n"
        f"exec timeout 4 tcpdump -c 4 -ni {capture_iface} -vv "
        f"'udp and dst port {port}'\n"
        "EOF\n"
        "chmod +x /tmp/nika_dscp_cap.sh\n"
        "nohup /tmp/nika_dscp_cap.sh >/tmp/nika_dscp_tos.out "
        "2>/tmp/nika_dscp_tos.err &",
    )
    time.sleep(0.5)
    runtime.exec(
        src_host,
        "python3 - <<'PY'\n"
        "import socket, time\n"
        f"tos={send_tos}\n"
        f"dst={dst_ip!r}\n"
        f"port={port}\n"
        "s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)\n"
        "for _ in range(12):\n"
        "    s.sendto(b'nika-dscp-probe', (dst, port))\n"
        "    time.sleep(0.05)\n"
        "s.close()\n"
        "PY",
    )
    time.sleep(2.5)
    out = runtime.exec(
        capture_host,
        "cat /tmp/nika_dscp_tos.err /tmp/nika_dscp_tos.out 2>/dev/null || true",
    )
    matches = re.findall(r"tos 0x([0-9a-fA-F]+)", out)
    if not matches:
        return None
    values = [int(m, 16) & 0xFC for m in matches]
    return max(set(values), key=values.count)


def stop(handle: CompeteHandle) -> None:
    """Stop EF/bulk iperf processes started for this matrix."""
    runtime = handle.runtime
    ports = {handle.matrix.ef.port, handle.matrix.ef.port + 50, _PROBE_PORT}
    hosts = {handle.matrix.ef.src_host, handle.matrix.ef.dst_host}
    for flow in handle.matrix.bulks:
        ports.add(flow.port)
        hosts.add(flow.src_host)
        hosts.add(flow.dst_host)
    for host in hosts:
        for port in ports:
            _kill_port_listeners(runtime, host, port)
        runtime.exec(host, "killall iperf3 2>/dev/null || true")
    system_logger.info(
        f"Stopped CORP QoS compete workload on "
        f"{handle.matrix.edge}/{handle.matrix.intf_name}"
    )


def pause_bulk(handle: CompeteHandle) -> None:
    """Stop bulk/EF background iperf clients; servers are restarted on resume."""
    runtime = handle.runtime
    hosts = {handle.matrix.ef.src_host, handle.matrix.ef.dst_host}
    for flow in handle.matrix.bulks:
        hosts.add(flow.src_host)
        hosts.add(flow.dst_host)
    for host in hosts:
        runtime.exec(host, "killall -q iperf3 2>/dev/null || true")


def resume_bulk(handle: CompeteHandle, *, duration_sec: int = 300) -> None:
    """Restart EF background + bulk clients after a DSCP sample window."""
    for flow in [handle.matrix.ef, *handle.matrix.bulks]:
        _start_iperf_server(handle.runtime, flow.dst_host, flow.port)
        _start_iperf_client(handle.runtime, flow, duration_sec)
    time.sleep(2.0)


def degraded(
    baseline: FlowMetrics,
    current: FlowMetrics,
    *,
    latency_factor: float = 5.0,
    jitter_factor: float = 5.0,
    loss_delta: float = 10.0,
    mdev_factor: float = 5.0,
    throughput_factor: float = 0.5,
) -> bool:
    """Return True when current EF metrics are clearly worse than baseline."""
    checks: list[bool] = []
    if baseline.latency_ms is not None and current.latency_ms is not None:
        base = max(baseline.latency_ms, 0.5)
        checks.append(current.latency_ms >= base * latency_factor)
    if baseline.latency_mdev_ms is not None and current.latency_mdev_ms is not None:
        base = max(baseline.latency_mdev_ms, 0.2)
        checks.append(current.latency_mdev_ms >= base * mdev_factor)
    if baseline.jitter_ms is not None and current.jitter_ms is not None:
        base = max(baseline.jitter_ms, 0.05)
        checks.append(current.jitter_ms >= base * jitter_factor)
    if baseline.lost_percent is not None and current.lost_percent is not None:
        checks.append(current.lost_percent >= baseline.lost_percent + loss_delta)
    if (
        baseline.bits_per_second is not None
        and current.bits_per_second is not None
        and baseline.bits_per_second > 0
    ):
        checks.append(
            current.bits_per_second <= baseline.bits_per_second * throughput_factor
        )
    # Demotion into a full BE queue often breaks iperf's TCP control path
    # entirely; treat a successful baseline with a failed/missing current
    # throughput sample as collapse.
    if baseline.bits_per_second is not None and baseline.bits_per_second > 0:
        if current.bits_per_second is None or (
            current.error and current.bits_per_second is None
        ):
            checks.append(True)
    return any(checks) if checks else False


__all__ = [
    "CompeteHandle",
    "CompeteMatrix",
    "FlowMetrics",
    "FlowSpec",
    "DSCP_CS0",
    "DSCP_EF",
    "TOS_CS0",
    "TOS_EF",
    "host_ip",
    "build_compete_matrix",
    "degraded",
    "measure",
    "pause_bulk",
    "resume_bulk",
    "sample_dscp_tos",
    "start",
    "stop",
]
