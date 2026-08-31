"""Container-side tshark inspection for capture files on lab nodes."""

from __future__ import annotations

import json
import re
import shlex
from typing import Any

from nika.runtime.base import LabRuntime
from nika.service.packet_capture.limits import clamp_inspect_limit, default_limits
from nika.service.packet_capture.models import InspectView
from nika.service.packet_capture.protocol_fields import extract_protocol_fields


class TsharkNotFoundError(RuntimeError):
    """Raised when a lab node has no tshark binary available."""


def require_tshark(runtime: LabRuntime, device: str) -> str:
    output = runtime.exec(device, "command -v tshark || true", timeout=5)
    path = output.strip()
    if not path:
        raise TsharkNotFoundError(
            f"Node {device!r} has no tshark installed for capture inspection. "
            "Use a nika/base or nika/frr node, or rebuild lab images with tshark."
        )
    return path


def tshark_version(runtime: LabRuntime, device: str) -> str | None:
    output = runtime.exec(
        device,
        "tshark -v 2>&1 | head -n 1 || true",
        timeout=5,
    )
    return output.strip().splitlines()[0] if output.strip() else None


def _run_tshark(
    runtime: LabRuntime,
    device: str,
    remote_path: str,
    args: list[str],
    *,
    timeout: float = 60.0,
) -> str:
    require_tshark(runtime, device)
    quoted = " ".join(shlex.quote(part) for part in args)
    output = runtime.exec(device, f"tshark {quoted} 2>/dev/null", timeout=timeout)
    if output.startswith("[TIMEOUT]"):
        raise RuntimeError("tshark timed out during capture inspection")
    return output


def count_packets(
    runtime: LabRuntime,
    device: str,
    remote_path: str,
    display_filter: str | None = None,
) -> int:
    args = ["-r", remote_path, "-T", "fields", "-e", "frame.number"]
    if display_filter:
        args.extend(["-Y", display_filter])
    output = _run_tshark(runtime, device, remote_path, args)
    numbers = [line.strip() for line in output.splitlines() if line.strip()]
    return len(numbers)


def inspect_capture(
    runtime: LabRuntime,
    device: str,
    remote_path: str,
    *,
    view: str,
    display_filter: str | None = None,
    protocol: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    view_name = InspectView(view)
    page_size = clamp_inspect_limit(limit)
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if view_name is InspectView.PROTOCOL and not protocol:
        raise ValueError("protocol is required for protocol view")

    total = count_packets(runtime, device, remote_path, display_filter)
    if view_name is InspectView.SUMMARY:
        data = _inspect_summary(runtime, device, remote_path, display_filter)
        return _envelope(
            view=view_name.value,
            data=data,
            returned=len(data.get("protocols", [])),
            total_available=total,
            offset=0,
            truncated=False,
        )

    if view_name is InspectView.EXPERT:
        data = _inspect_expert(runtime, device, remote_path, display_filter)
        items = data.get("items", [])
        return _envelope(
            view=view_name.value,
            data=data,
            returned=len(items),
            total_available=len(items),
            offset=0,
            truncated=False,
        )

    if view_name is InspectView.PROTOCOL:
        items, returned, truncated = _inspect_protocol(
            runtime,
            device,
            remote_path,
            protocol=protocol,
            display_filter=display_filter,
            limit=page_size,
            offset=offset,
        )
        return _envelope(
            view=view_name.value,
            data={"protocol": protocol, "packets": items},
            returned=returned,
            total_available=total,
            offset=offset,
            truncated=truncated,
        )

    items, returned, truncated = _inspect_packets(
        runtime,
        device,
        remote_path,
        display_filter=display_filter,
        limit=page_size,
        offset=offset,
    )
    return _envelope(
        view=view_name.value,
        data={"packets": items},
        returned=returned,
        total_available=total,
        offset=offset,
        truncated=truncated,
    )


def _envelope(
    *,
    view: str,
    data: dict[str, Any],
    returned: int,
    total_available: int,
    offset: int,
    truncated: bool,
) -> dict[str, Any]:
    return {
        "view": view,
        "truncated": truncated,
        "returned": returned,
        "total_available": total_available,
        "offset": offset,
        "data": data,
    }


def _inspect_summary(
    runtime: LabRuntime,
    device: str,
    remote_path: str,
    display_filter: str | None,
) -> dict[str, Any]:
    args = ["-r", remote_path, "-q", "-z", "io,phs"]
    if display_filter:
        args.extend(["-Y", display_filter])
    phs_output = _run_tshark(runtime, device, remote_path, args)
    protocols = _parse_io_phs(phs_output)

    conv_args = ["-r", remote_path, "-q", "-z", "conv,ip"]
    if display_filter:
        conv_args.extend(["-Y", display_filter])
    conv_output = _run_tshark(runtime, device, remote_path, conv_args)
    conversations = _parse_conversations(conv_output)

    time_args = ["-r", remote_path, "-T", "fields", "-e", "frame.time_epoch"]
    if display_filter:
        time_args.extend(["-Y", display_filter])
    times = [
        float(value)
        for value in _run_tshark(runtime, device, remote_path, time_args).splitlines()
        if value.strip()
    ]
    time_range = None
    if times:
        time_range = {"start_epoch": min(times), "end_epoch": max(times)}

    endpoint_args = ["-r", remote_path, "-q", "-z", "endpoints,ip"]
    if display_filter:
        endpoint_args.extend(["-Y", display_filter])
    endpoint_output = _run_tshark(runtime, device, remote_path, endpoint_args)
    endpoints = _parse_endpoints(endpoint_output)

    return {
        "protocols": protocols,
        "endpoints": endpoints,
        "conversations": conversations,
        "time_range": time_range,
        "tshark_version": tshark_version(runtime, device),
        "include_payload": default_limits().include_payload,
    }


def _parse_io_phs(output: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = re.match(r"^\s*([^\s]+)\s+frames:(\d+)\s+bytes:(\d+)", line)
        if not match:
            continue
        rows.append(
            {
                "protocol": match.group(1),
                "frames": int(match.group(2)),
                "bytes": int(match.group(3)),
            }
        )
    return rows


def _parse_endpoints(output: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].count(".") != 3:
            continue
        try:
            rows.append(
                {
                    "address": parts[0],
                    "frames": int(parts[1]),
                    "bytes": int(parts[2]),
                }
            )
        except ValueError:
            continue
    return rows[:50]


def _parse_conversations(output: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 7:
            continue
        if parts[0].count(".") != 3 or parts[2].count(".") != 3:
            continue
        try:
            rows.append(
                {
                    "address_a": parts[0],
                    "port_a": parts[1],
                    "address_b": parts[2],
                    "port_b": parts[3],
                    "frames": int(parts[4]),
                    "bytes": int(parts[5]),
                }
            )
        except ValueError:
            continue
    return rows[:50]


def _inspect_packets(
    runtime: LabRuntime,
    device: str,
    remote_path: str,
    *,
    display_filter: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int, bool]:
    args = [
        "-r",
        remote_path,
        "-T",
        "fields",
        "-E",
        "separator=|",
        "-e",
        "frame.number",
        "-e",
        "frame.time_relative",
        "-e",
        "frame.len",
        "-e",
        "_ws.col.Protocol",
        "-e",
        "ip.src",
        "-e",
        "ip.dst",
        "-e",
        "ipv6.src",
        "-e",
        "ipv6.dst",
        "-e",
        "_ws.col.Info",
    ]
    if display_filter:
        args.extend(["-Y", display_filter])
    output = _run_tshark(runtime, device, remote_path, args)
    rows = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        while len(parts) < 9:
            parts.append("")
        rows.append(
            {
                "number": parts[0],
                "time_relative": parts[1],
                "length": parts[2],
                "protocol": parts[3],
                "src": parts[4] or parts[6],
                "dst": parts[5] or parts[7],
                "info": parts[8],
            }
        )
    page = rows[offset : offset + limit]
    truncated = offset + limit < len(rows)
    return page, len(page), truncated


def _inspect_protocol(
    runtime: LabRuntime,
    device: str,
    remote_path: str,
    *,
    protocol: str,
    display_filter: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int, bool]:
    filter_expr = (
        protocol if not display_filter else f"({display_filter}) and {protocol}"
    )
    args = ["-r", remote_path, "-T", "json"]
    if filter_expr:
        args.extend(["-Y", filter_expr])
    raw = _run_tshark(runtime, device, remote_path, args, timeout=90.0)
    packets = json.loads(raw or "[]")
    page = packets[offset : offset + limit]
    normalized = [extract_protocol_fields(protocol, packet) for packet in page]
    truncated = offset + limit < len(packets)
    return normalized, len(normalized), truncated


def _inspect_expert(
    runtime: LabRuntime,
    device: str,
    remote_path: str,
    display_filter: str | None,
) -> dict[str, Any]:
    args = ["-r", remote_path, "-q", "-z", "expert"]
    if display_filter:
        args.extend(["-Y", display_filter])
    output = _run_tshark(runtime, device, remote_path, args)
    items: list[dict[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("="):
            continue
        if "Expert Info" in line:
            continue
        match = re.match(
            r"^\s*(\d+)\s+(\S+)\s+(\S+)\s+(.+)$",
            line,
        )
        if match:
            items.append(
                {
                    "frame": match.group(1),
                    "severity": match.group(2),
                    "group": match.group(3),
                    "summary": match.group(4).strip(),
                }
            )
            continue
        if len(line) > 10:
            items.append({"summary": line})
    return {"items": items[:200]}
