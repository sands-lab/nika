"""Hard safety ceilings for packet capture; agents set limits via MCP tool args."""

from __future__ import annotations

from nika.service.packet_capture.models import CaptureLimits

# Safety ceilings. Agents choose values through packet_capture_* tool parameters;
# omitted args fall back to these defaults and cannot exceed the ceilings.
HARD_MAX_DURATION_SEC = 30.0
HARD_MAX_PACKETS = 2000
HARD_MAX_BYTES: int | None = None
HARD_INSPECT_PAGE_SIZE = 50
INCLUDE_PAYLOAD = False


def default_limits() -> CaptureLimits:
    return CaptureLimits(
        max_duration_sec=HARD_MAX_DURATION_SEC,
        max_packets=HARD_MAX_PACKETS,
        max_bytes=HARD_MAX_BYTES,
        inspect_page_size=HARD_INSPECT_PAGE_SIZE,
        include_payload=INCLUDE_PAYLOAD,
    )


def clamp_start_limits(
    *,
    max_duration_sec: float | None,
    max_packets: int | None,
    max_bytes: int | None,
) -> CaptureLimits:
    """Apply agent-requested limits under hard safety ceilings."""
    ceilings = default_limits()
    duration = (
        ceilings.max_duration_sec if max_duration_sec is None else max_duration_sec
    )
    packets = ceilings.max_packets if max_packets is None else max_packets
    bytes_cap = ceilings.max_bytes if max_bytes is None else max_bytes

    if duration <= 0:
        raise ValueError("max_duration_sec must be positive")
    if packets <= 0:
        raise ValueError("max_packets must be positive")
    if bytes_cap is not None and bytes_cap <= 0:
        raise ValueError("max_bytes must be positive when set")

    duration = min(duration, ceilings.max_duration_sec)
    packets = min(packets, ceilings.max_packets)
    if ceilings.max_bytes is not None:
        if bytes_cap is None:
            bytes_cap = ceilings.max_bytes
        else:
            bytes_cap = min(bytes_cap, ceilings.max_bytes)

    return CaptureLimits(
        max_duration_sec=duration,
        max_packets=packets,
        max_bytes=bytes_cap,
        inspect_page_size=ceilings.inspect_page_size,
        include_payload=ceilings.include_payload,
    )


def clamp_inspect_limit(limit: int | None) -> int:
    page = HARD_INSPECT_PAGE_SIZE if limit is None else limit
    if page <= 0:
        raise ValueError("limit must be positive")
    return min(page, HARD_INSPECT_PAGE_SIZE)
