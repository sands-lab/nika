"""Host-side tshark inspection for stored capture artifacts."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from nika.service.packet_capture.limits import clamp_inspect_limit, default_limits
from nika.service.packet_capture.models import InspectView
from nika.service.packet_capture.protocol_fields import extract_protocol_fields


class TsharkNotFoundError(RuntimeError):
    """Raised when the host tshark binary is unavailable."""


def require_tshark() -> str:
    path = shutil.which("tshark")
    if not path:
        raise TsharkNotFoundError(
            "tshark is not installed on the NIKA host. Install wireshark-common/tshark to inspect captures."
        )
    return path


def tshark_version() -> str | None:
    path = shutil.which("tshark")
    if not path:
        return None
    try:
        output = subprocess.check_output(
            [path, "-v"],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return output.strip().splitlines()[0] if output.strip() else None


def _run_tshark(args: list[str], *, timeout: float = 60.0) -> str:
    tshark = require_tshark()
    try:
        return subprocess.check_output(
            [tshark, *args],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(exc.output.strip() or str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("tshark timed out during capture inspection") from exc


def count_packets(pcap_path: Path, display_filter: str | None = None) -> int:
    args = ["-r", str(pcap_path), "-T", "fields", "-e", "frame.number"]
    if display_filter:
        args.extend(["-Y", display_filter])
    output = _run_tshark(args)
    numbers = [line.strip() for line in output.splitlines() if line.strip()]
    return len(numbers)


def inspect_capture(
    pcap_path: Path,
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

    total = count_packets(pcap_path, display_filter)
    if view_name is InspectView.SUMMARY:
        data = _inspect_summary(pcap_path, display_filter)
        return _envelope(
            view=view_name.value,
            data=data,
            returned=len(data.get("protocols", [])),
            total_available=total,
            offset=0,
            truncated=False,
        )

    if view_name is InspectView.EXPERT:
        data = _inspect_expert(pcap_path, display_filter)
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
            pcap_path,
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
        pcap_path,
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


def _inspect_summary(pcap_path: Path, display_filter: str | None) -> dict[str, Any]:
    args = ["-r", str(pcap_path), "-q", "-z", "io,phs"]
    if display_filter:
        args.extend(["-Y", display_filter])
    phs_output = _run_tshark(args)
    protocols = _parse_io_phs(phs_output)

    conv_args = ["-r", str(pcap_path), "-q", "-z", "conv,ip"]
    if display_filter:
        conv_args.extend(["-Y", display_filter])
    conv_output = _run_tshark(conv_args)
    conversations = _parse_conversations(conv_output)

    time_args = ["-r", str(pcap_path), "-T", "fields", "-e", "frame.time_epoch"]
    if display_filter:
        time_args.extend(["-Y", display_filter])
    times = [
        float(value) for value in _run_tshark(time_args).splitlines() if value.strip()
    ]
    time_range = None
    if times:
        time_range = {"start_epoch": min(times), "end_epoch": max(times)}

    endpoint_args = ["-r", str(pcap_path), "-q", "-z", "endpoints,ip"]
    if display_filter:
        endpoint_args.extend(["-Y", display_filter])
    endpoint_output = _run_tshark(endpoint_args)
    endpoints = _parse_endpoints(endpoint_output)

    return {
        "protocols": protocols,
        "endpoints": endpoints,
        "conversations": conversations,
        "time_range": time_range,
        "tshark_version": tshark_version(),
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
    pcap_path: Path,
    *,
    display_filter: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int, bool]:
    args = [
        "-r",
        str(pcap_path),
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
    output = _run_tshark(args)
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
    pcap_path: Path,
    *,
    protocol: str,
    display_filter: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict[str, Any]], int, bool]:
    filter_expr = (
        protocol if not display_filter else f"({display_filter}) and {protocol}"
    )
    args = ["-r", str(pcap_path), "-T", "json"]
    if filter_expr:
        args.extend(["-Y", filter_expr])
    raw = _run_tshark(args, timeout=90.0)
    packets = json.loads(raw or "[]")
    page = packets[offset : offset + limit]
    normalized = [extract_protocol_fields(protocol, packet) for packet in page]
    truncated = offset + limit < len(packets)
    return normalized, len(normalized), truncated


def _inspect_expert(pcap_path: Path, display_filter: str | None) -> dict[str, Any]:
    args = ["-r", str(pcap_path), "-q", "-z", "expert"]
    if display_filter:
        args.extend(["-Y", display_filter])
    output = _run_tshark(args)
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
