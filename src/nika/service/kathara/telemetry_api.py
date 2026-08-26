import json
from datetime import datetime

from nika.service.kathara.base_api import KatharaBaseAPI, _SupportsBase


class TelemetryAPIMixin:
    """Query observed INT-MX telemetry from a Kathara collector."""

    @staticmethod
    def _time_bound_ns(value: str) -> int:
        """Normalize Unix seconds, Unix nanoseconds, or an ISO-8601 value."""
        try:
            numeric = float(value)
        except ValueError:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(parsed.timestamp() * 1_000_000_000)
        if abs(numeric) < 1_000_000_000_000:
            numeric *= 1_000_000_000
        return int(numeric)

    def int_query_telemetry(
        self: _SupportsBase,
        start_time: str,
        end_time: str | None = None,
        src: str | None = None,
        dst: str | None = None,
        protocol: str | None = None,
        src_port: int | None = None,
        dst_port: int | None = None,
        flow_id: str | None = None,
        packet_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Query observed INT-MX traces from the scenario collector."""
        start_ns = self._time_bound_ns(start_time)
        end_ns = self._time_bound_ns(end_time) if end_time is not None else None
        raw = self.exec_cmd(
            "collector",
            "tail -n 10000 /var/lib/nika/int_reports.jsonl 2>/dev/null || true",
            timeout=20,
        )
        rows: list[dict] = []
        for line in raw.splitlines():
            try:
                row = json.loads(line)
                timestamp_ns = int(row["packet_timestamp"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if timestamp_ns < start_ns or (
                end_ns is not None and timestamp_ns > end_ns
            ):
                continue
            filters = {
                "src": src,
                "dst": dst,
                "protocol": protocol,
                "src_port": src_port,
                "dst_port": dst_port,
                "flow_id": flow_id,
                "packet_id": packet_id,
            }
            if any(
                value is not None and str(row.get(key)) != str(value)
                for key, value in filters.items()
            ):
                continue
            rows.append(row)
        return rows[-max(1, min(limit, 1000)) :]


class KatharaTelemetryAPI(KatharaBaseAPI, TelemetryAPIMixin):
    """Kathara API for observed INT-MX telemetry."""

    pass
