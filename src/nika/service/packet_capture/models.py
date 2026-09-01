"""Data models for packet capture sessions."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal

CaptureStatus = Literal["running", "stopped", "failed"]
CaptureBackendKind = Literal["dumpcap", "tcpdump"]
InspectViewName = Literal["summary", "packets", "protocol", "expert"]


class InspectView(str, Enum):
    SUMMARY = "summary"
    PACKETS = "packets"
    PROTOCOL = "protocol"
    EXPERT = "expert"


@dataclass
class CaptureLimits:
    max_duration_sec: float = 30.0
    max_packets: int = 2000
    max_bytes: int | None = None
    inspect_page_size: int = 50
    include_payload: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CaptureMeta:
    capture_id: str
    device: str
    interface: str
    capture_filter: str | None
    remote_path: str
    pid_path: str
    capture_backend: CaptureBackendKind
    limits: CaptureLimits
    status: CaptureStatus = "running"
    started_at: str = ""
    stopped_at: str | None = None
    packet_count: int | None = None
    captured_bytes: int | None = None
    dropped_packets: int | None = None
    dumpcap_version: str | None = None
    tshark_version: str | None = None
    inspect_display_filters: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["limits"] = self.limits.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CaptureMeta:
        limits_raw = data.get("limits") or {}
        limits = CaptureLimits(**limits_raw)
        return cls(
            capture_id=str(data["capture_id"]),
            device=str(data["device"]),
            interface=str(data["interface"]),
            capture_filter=data.get("capture_filter"),
            remote_path=str(data["remote_path"]),
            pid_path=str(data["pid_path"]),
            capture_backend=data.get("capture_backend", "tcpdump"),
            limits=limits,
            status=data.get("status", "running"),
            started_at=str(data.get("started_at") or ""),
            stopped_at=data.get("stopped_at"),
            packet_count=data.get("packet_count"),
            captured_bytes=data.get("captured_bytes"),
            dropped_packets=data.get("dropped_packets"),
            dumpcap_version=data.get("dumpcap_version"),
            tshark_version=data.get("tshark_version"),
            inspect_display_filters=list(data.get("inspect_display_filters") or []),
        )
