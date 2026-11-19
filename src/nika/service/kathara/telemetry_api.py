import json
import shlex
import time
from datetime import datetime
from pathlib import Path

from nika.net_env.p4_dc_gateway.topology_model import INT_REPORTS
from nika.service.lab.checked_exec import checked_exec
from nika.service.kathara.base_api import KatharaBaseAPI, _SupportsBase
from nika.service.lab import int_query
from nika.service.lab.int_query import (
    INCOMPLETE_GRACE_NS as INCOMPLETE_GRACE_NS,
    collapse_traces as collapse_traces,
)


def time_bound_seconds(value: str) -> float:
    """Return a time bound of the flow and metric tools as Unix seconds.

    The bound is Unix seconds or nanoseconds, ISO-8601, or ``-N`` for N seconds
    ago.
    """
    if value.startswith("-") and value[1:].replace(".", "", 1).isdigit():
        return time.time() - float(value[1:])
    return TelemetryAPIMixin._time_bound_ns(value) / 1_000_000_000


class TelemetryAPIMixin:
    """Query observed INT-MD telemetry from a Kathara collector."""

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
        offset: int = 0,
        egress_switch: str | None = None,
        egress_port: int | None = None,
    ) -> list[dict]:
        """Query observed INT-MD traces from the scenario collector."""
        start_ns = self._time_bound_ns(start_time)
        end_ns = self._time_bound_ns(end_time) if end_time is not None else None
        if offset < 0:
            raise ValueError("offset must be nonnegative")
        query = {
            "path": INT_REPORTS,
            "start_ns": start_ns,
            "end_ns": end_ns,
            "filters": {
                "src": src,
                "dst": dst,
                "protocol": protocol,
                "src_port": src_port,
                "dst_port": dst_port,
                "flow_id": flow_id,
                "packet_id": packet_id,
            },
            "egress_switch": egress_switch,
            "egress_port": egress_port,
            "limit": limit,
            "offset": offset,
        }
        program = Path(int_query.__file__).read_text()
        raw = checked_exec(
            self.exec_cmd,
            "collector",
            f"python3 -c {shlex.quote(program)} {shlex.quote(json.dumps(query))}",
            timeout=20,
        )
        try:
            rows = json.loads(raw)
        except ValueError as exc:
            raise RuntimeError(
                f"INT collector returned invalid query JSON: {raw}"
            ) from exc
        if not isinstance(rows, list):
            raise RuntimeError(f"INT collector returned invalid query rows: {raw}")
        return rows


class KatharaTelemetryAPI(KatharaBaseAPI, TelemetryAPIMixin):
    """Kathara API for observed INT-MD telemetry."""

    pass
