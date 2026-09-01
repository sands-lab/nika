"""Node-side packet capture using dumpcap or tcpdump."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from datetime import datetime, timezone

from nika.runtime.base import LabRuntime
from nika.service.packet_capture.models import CaptureBackendKind, CaptureLimits


@dataclass
class RunningCapture:
    capture_id: str
    device: str
    interface: str
    remote_path: str
    pid_path: str
    capture_backend: CaptureBackendKind
    started_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _shell_quote(value: str) -> str:
    return shlex.quote(value)


def detect_capture_backend(runtime: LabRuntime, device: str) -> CaptureBackendKind:
    output = runtime.exec(device, "command -v dumpcap || true", timeout=5)
    if output.strip():
        return "dumpcap"
    output = runtime.exec(device, "command -v tcpdump || true", timeout=5)
    if output.strip():
        return "tcpdump"
    raise RuntimeError(
        f"Node {device!r} has neither dumpcap nor tcpdump installed for packet capture"
    )


def remote_paths(capture_id: str) -> tuple[str, str]:
    remote_path = f"/tmp/nika-capture-{capture_id}.pcapng"
    pid_path = f"/tmp/nika-capture-{capture_id}.pid"
    return remote_path, pid_path


def start_capture(
    runtime: LabRuntime,
    *,
    capture_id: str,
    device: str,
    interface: str,
    capture_filter: str | None,
    limits: CaptureLimits,
) -> RunningCapture:
    backend = detect_capture_backend(runtime, device)
    remote_path, pid_path = remote_paths(capture_id)
    quoted_iface = _shell_quote(interface)
    quoted_remote = _shell_quote(remote_path)
    quoted_pid = _shell_quote(pid_path)

    runtime.exec(
        device,
        f"rm -f {quoted_remote} {quoted_pid}; "
        f"kill $(cat {quoted_pid} 2>/dev/null) 2>/dev/null || true",
        timeout=10,
    )

    bpf = capture_filter.strip() if capture_filter else ""
    if backend == "dumpcap":
        cmd_parts = [
            "dumpcap",
            "-i",
            interface,
            "-w",
            remote_path,
            "-q",
            "-a",
            f"duration:{int(limits.max_duration_sec)}",
            "-c",
            str(limits.max_packets),
        ]
        if limits.max_bytes is not None:
            cmd_parts.extend(["-a", f"filesize:{limits.max_bytes}"])
        if bpf:
            cmd_parts.extend(["-f", bpf])
        capture_cmd = " ".join(shlex.quote(part) for part in cmd_parts)
    else:
        capture_cmd = (
            f"timeout {int(limits.max_duration_sec)} tcpdump -U -i {quoted_iface} "
            f"-w {quoted_remote} -c {limits.max_packets}"
        )
        if bpf:
            capture_cmd += f" {_shell_quote(bpf)}"

    launch = (
        f"nohup sh -c {shlex.quote(capture_cmd)} >/tmp/nika-capture-{capture_id}.log "
        f"2>&1 & echo $! > {quoted_pid}"
    )
    runtime.exec(device, launch, timeout=15)
    pid = runtime.exec(device, f"cat {quoted_pid}", timeout=5).strip()
    if not pid.isdigit():
        log_tail = runtime.exec(
            device,
            f"tail -n 20 /tmp/nika-capture-{capture_id}.log 2>/dev/null || true",
            timeout=5,
        )
        raise RuntimeError(f"Failed to start capture on {device}: {log_tail.strip()}")

    return RunningCapture(
        capture_id=capture_id,
        device=device,
        interface=interface,
        remote_path=remote_path,
        pid_path=pid_path,
        capture_backend=backend,
        started_at=_utc_now(),
    )


@dataclass
class StopStats:
    packet_count: int
    captured_bytes: int
    dropped_packets: int
    dumpcap_version: str | None


def _parse_int(value: str) -> int | None:
    value = value.strip()
    return int(value) if value.isdigit() else None


def stop_capture(
    runtime: LabRuntime, *, device: str, pid_path: str, remote_path: str
) -> StopStats:
    quoted_pid = _shell_quote(pid_path)
    quoted_remote = _shell_quote(remote_path)
    runtime.exec(
        device,
        f"if [ -f {quoted_pid} ]; then kill $(cat {quoted_pid}) 2>/dev/null || true; fi",
        timeout=10,
    )
    runtime.exec(device, "sleep 0.5", timeout=5)

    size_raw = runtime.exec(
        device,
        f"wc -c < {quoted_remote} 2>/dev/null || echo 0",
        timeout=10,
    )
    captured_bytes = _parse_int(size_raw) or 0

    packet_count = 0
    count_output = runtime.exec(
        device,
        f"tcpdump -r {quoted_remote} -n 2>/dev/null | wc -l",
        timeout=30,
    )
    packet_count = _parse_int(count_output) or 0

    dropped_packets = 0
    log_output = runtime.exec(
        device,
        "grep -E 'dropped|captured' /tmp/nika-capture-*.log 2>/dev/null | tail -n 1 || true",
        timeout=5,
    )
    dropped_match = re.search(r"(\d+)\s+packets?\s+dropped", log_output, re.IGNORECASE)
    if dropped_match:
        dropped_packets = int(dropped_match.group(1))

    dumpcap_version = None
    version_output = runtime.exec(
        device, "dumpcap -v 2>&1 | head -n 1 || true", timeout=5
    )
    if version_output.strip():
        dumpcap_version = version_output.strip().splitlines()[0]

    return StopStats(
        packet_count=packet_count,
        captured_bytes=captured_bytes,
        dropped_packets=dropped_packets,
        dumpcap_version=dumpcap_version,
    )
