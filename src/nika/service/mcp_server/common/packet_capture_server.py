"""MCP tools for session-scoped packet capture and bounded inspection."""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from nika.service.mcp_server.session_context import (
    get_session_dir,
    get_session_meta,
)
from nika.runtime.factory import runtime_for_session
from nika.service.packet_capture.limits import (
    HARD_INSPECT_PAGE_SIZE,
    HARD_MAX_DURATION_SEC,
    HARD_MAX_PACKETS,
)
from nika.service.packet_capture.manager import CaptureManager
from nika.utils.errors import safe_tool

mcp = FastMCP("packet_capture_mcp_server")


def _manager() -> CaptureManager:
    runtime = runtime_for_session(get_session_meta())
    return CaptureManager(session_dir=get_session_dir(), runtime=runtime)


@safe_tool
@mcp.tool()
def packet_capture_start(
    device: str,
    interface: str,
    capture_filter: str | None = None,
    max_duration_sec: float = HARD_MAX_DURATION_SEC,
    max_packets: int = HARD_MAX_PACKETS,
    max_bytes: int = 0,
) -> str:
    """Start an asynchronous packet capture on a lab device interface.

    Use a BPF capture filter (libpcap syntax) to limit what is recorded.
    Defaults: max_duration_sec=30, max_packets=2000, max_bytes=0 (no byte cap).
    Returns a capture_id for stop/inspect. Run probes or traffic while capturing.
    """
    result = _manager().start(
        device=device,
        interface=interface,
        capture_filter=capture_filter,
        max_duration_sec=max_duration_sec,
        max_packets=max_packets,
        max_bytes=None if max_bytes <= 0 else max_bytes,
    )
    return json.dumps(result, indent=2)


@safe_tool
@mcp.tool()
def packet_capture_stop(capture_id: str) -> str:
    """Stop a running capture and persist the pcapng artifact with metadata."""
    result = _manager().stop(capture_id)
    return json.dumps(result, indent=2)


@safe_tool
@mcp.tool()
def packet_capture_inspect(
    capture_id: str,
    view: str,
    display_filter: str | None = None,
    protocol: str | None = None,
    limit: int = HARD_INSPECT_PAGE_SIZE,
    offset: int = 0,
) -> str:
    """Inspect a stopped capture with bounded, paginated packet evidence.

    Views:
      - summary: protocol distribution, endpoints, conversations, time range
      - packets: paginated frame summaries without application payload
      - protocol: protocol-specific fields (requires protocol=bgp|ospf|tcp|dns|...)
      - expert: Wireshark expert evidence such as retransmissions and malformed frames

    display_filter uses Wireshark display filter syntax (not BPF).
    Defaults: limit=50, offset=0.
    """
    result = _manager().inspect(
        capture_id,
        view=view,
        display_filter=display_filter,
        protocol=protocol,
        limit=limit,
        offset=offset,
    )
    return json.dumps(result, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
