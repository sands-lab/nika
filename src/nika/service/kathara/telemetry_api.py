import json
import time
from datetime import datetime

from nika.net_env.p4_dc_gateway.topology_model import INT_REPORTS
from nika.service.kathara.base_api import KatharaBaseAPI, _SupportsBase


# A trace without its sink hop is still in flight until this much time passes.
INCOMPLETE_GRACE_NS = 2_000_000_000


def collapse_traces(
    records: list[dict], now_ns: int, grace_ns: int = INCOMPLETE_GRACE_NS
) -> list[dict]:
    """Return one record per (flow, packet id): the latest one written.

    The collector appends a record per hop report, so a packet appears once
    per hop with a growing ``hop_sequence``. Traces that have no sink yet and
    are younger than ``grace_ns`` are still in flight and are omitted.
    """
    latest: dict[tuple[str, str], dict] = {}
    for row in records:
        latest[(str(row.get("flow_id")), str(row.get("packet_id")))] = row
    return [
        row
        for row in latest.values()
        if row.get("trace_complete")
        or now_ns - int(row["packet_timestamp"]) >= grace_ns
    ]


def time_bound_seconds(value: str) -> float:
    """Return a time bound of the flow and metric tools as Unix seconds.

    The bound is Unix seconds or nanoseconds, ISO-8601, or ``-N`` for N seconds
    ago.
    """
    if value.startswith("-") and value[1:].replace(".", "", 1).isdigit():
        return time.time() - float(value[1:])
    return TelemetryAPIMixin._time_bound_ns(value) / 1_000_000_000


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
            f"date +%s%N; tail -n 10000 {INT_REPORTS} 2>/dev/null || true",
            timeout=20,
        )
        lines = raw.splitlines()
        try:
            now_ns = int(lines[0])
        except (IndexError, ValueError):
            return []
        records: list[dict] = []
        for line in lines[1:]:
            try:
                row = json.loads(line)
                int(row["packet_timestamp"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            records.append(row)
        rows: list[dict] = []
        for row in collapse_traces(records, now_ns):
            timestamp_ns = int(row["packet_timestamp"])
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
