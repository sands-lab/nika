"""Session-scoped packet capture lifecycle management."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from nika.runtime.base import LabRuntime
from nika.service.packet_capture import artifact, capture_backend, inspect
from nika.service.packet_capture.limits import clamp_start_limits
from nika.service.packet_capture.models import CaptureMeta


class CaptureManager:
    """Manage concurrent packet captures for one troubleshooting session."""

    def __init__(self, *, session_dir: str, runtime: LabRuntime) -> None:
        self.session_dir = session_dir
        self.runtime = runtime

    def start(
        self,
        *,
        device: str,
        interface: str,
        capture_filter: str | None = None,
        max_duration_sec: float | None = None,
        max_packets: int | None = None,
        max_bytes: int | None = None,
    ) -> dict:
        limits = clamp_start_limits(
            max_duration_sec=max_duration_sec,
            max_packets=max_packets,
            max_bytes=max_bytes,
        )
        capture_id = uuid.uuid4().hex
        running = capture_backend.start_capture(
            self.runtime,
            capture_id=capture_id,
            device=device,
            interface=interface,
            capture_filter=capture_filter,
            limits=limits,
        )
        meta = CaptureMeta(
            capture_id=capture_id,
            device=device,
            interface=interface,
            capture_filter=capture_filter,
            remote_path=running.remote_path,
            pid_path=running.pid_path,
            capture_backend=running.capture_backend,
            limits=limits,
            status="running",
            started_at=running.started_at,
        )
        artifact.write_meta(self.session_dir, meta.to_dict())
        return {
            "capture_id": capture_id,
            "status": "running",
            "started_at": running.started_at,
            "limits_applied": limits.to_dict(),
        }

    def stop(self, capture_id: str) -> dict:
        meta = CaptureMeta.from_dict(artifact.read_meta(self.session_dir, capture_id))
        if meta.status == "stopped":
            return self._stop_payload(meta)

        stats = capture_backend.stop_capture(
            self.runtime,
            device=meta.device,
            pid_path=meta.pid_path,
            remote_path=meta.remote_path,
        )
        local_path, digest = artifact.store_artifact(
            self.runtime,
            session_dir=self.session_dir,
            capture_id=capture_id,
            device=meta.device,
            remote_path=meta.remote_path,
        )
        stopped_at = datetime.now(timezone.utc).isoformat()
        meta.status = "stopped"
        meta.stopped_at = stopped_at
        meta.packet_count = stats.packet_count
        meta.captured_bytes = stats.captured_bytes
        meta.dropped_packets = stats.dropped_packets
        meta.dumpcap_version = stats.dumpcap_version
        meta.tshark_version = inspect.tshark_version()
        meta.sha256 = digest
        meta.artifact_path = str(
            Path("packet_captures") / capture_id / "capture.pcapng"
        )
        artifact.write_meta(self.session_dir, meta.to_dict())
        return self._stop_payload(meta, local_path=local_path)

    def inspect(
        self,
        capture_id: str,
        *,
        view: str,
        display_filter: str | None = None,
        protocol: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict:
        meta = CaptureMeta.from_dict(artifact.read_meta(self.session_dir, capture_id))
        if meta.status != "stopped":
            raise RuntimeError(
                f"Capture {capture_id!r} is not stopped yet; call packet_capture_stop first"
            )
        pcap_path = artifact.artifact_file_path(self.session_dir, capture_id)
        if not pcap_path.is_file():
            raise FileNotFoundError(f"Capture artifact missing for {capture_id!r}")

        if display_filter and display_filter not in meta.inspect_display_filters:
            meta.inspect_display_filters.append(display_filter)
            artifact.write_meta(self.session_dir, meta.to_dict())

        payload = inspect.inspect_capture(
            pcap_path,
            view=view,
            display_filter=display_filter,
            protocol=protocol,
            limit=limit,
            offset=offset,
        )
        payload["capture_id"] = capture_id
        return payload

    @staticmethod
    def _stop_payload(meta: CaptureMeta, local_path: Path | None = None) -> dict:
        duration_sec = 0.0
        if meta.started_at and meta.stopped_at:
            start = datetime.fromisoformat(meta.started_at)
            stop = datetime.fromisoformat(meta.stopped_at)
            duration_sec = max(0.0, (stop - start).total_seconds())
        artifact_info = {
            "path": meta.artifact_path,
            "sha256": meta.sha256,
            "dumpcap_version": meta.dumpcap_version,
            "tshark_version": meta.tshark_version,
        }
        if local_path is not None:
            artifact_info["local_path"] = str(local_path)
        return {
            "capture_id": meta.capture_id,
            "packet_count": meta.packet_count or 0,
            "captured_bytes": meta.captured_bytes or 0,
            "duration_sec": duration_sec,
            "dropped_packets": meta.dropped_packets or 0,
            "artifact": artifact_info,
        }
