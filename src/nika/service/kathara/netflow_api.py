import shlex
import time
from datetime import UTC, datetime

from nika.service.kathara.base_api import KatharaBaseAPI, _SupportsBase
from nika.service.kathara.telemetry_api import TelemetryAPIMixin
from nika.telemetry.netflow import FLOW_COLLECTOR, FLOW_DIR, FLOW_EXPORTERS_FILE

# Agent-facing aggregation keys mapped to nfdump ``-A`` keys and output fields.
NETFLOW_AGGREGATE_KEYS: dict[str, tuple[str, str, str]] = {
    "router": ("router", "%ra", "exporter"),
    "inif": ("inif", "%in", "in_if"),
    "proto": ("proto", "%pr", "proto"),
    "srcip": ("srcip", "%sa", "src_ip"),
    "srcport": ("srcport", "%sp", "src_port"),
    "dstip": ("dstip", "%da", "dst_ip"),
    "dstport": ("dstport", "%dp", "dst_port"),
}
NETFLOW_ORDER_KEYS = ("bytes", "packets", "flows")
NETFLOW_MAX_LIMIT = 100
NETFLOW_MAX_FILTER_CHARS = 512

_RECORD_FIELDS = (
    ("%ts", "first_seen"),
    ("%te", "last_seen"),
    ("%ra", "exporter"),
    ("%in", "in_if"),
    ("%pr", "proto"),
    ("%sa", "src_ip"),
    ("%sp", "src_port"),
    ("%da", "dst_ip"),
    ("%dp", "dst_port"),
)
_COUNTER_FIELDS = (("%pkt", "packets"), ("%byt", "bytes"), ("%fl", "flows"))
_INT_FIELDS = {"in_if", "src_port", "dst_port", "packets", "bytes", "flows"}
_PROTO_NAMES = {
    "1": "icmp",
    "2": "igmp",
    "6": "tcp",
    "17": "udp",
    "47": "gre",
    "89": "ospf",
}


def _nfdump_time(value: str) -> str:
    """Format a time bound as an nfdump ``-t`` UTC timestamp.

    Accepts what ``int_query_telemetry`` accepts, plus ``-N`` for N seconds ago.
    """
    if value.startswith("-") and value[1:].replace(".", "", 1).isdigit():
        seconds = time.time() - float(value[1:])
    else:
        seconds = TelemetryAPIMixin._time_bound_ns(value) / 1_000_000_000
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y/%m/%d.%H:%M:%S")


class NetflowAPIMixin:
    """Query IPFIX flow records stored by the lab flow collector."""

    def _flow_exporters(self: _SupportsBase) -> dict[str, str]:
        raw = self.exec_cmd(FLOW_COLLECTOR, f"cat {FLOW_EXPORTERS_FILE}")
        exporters: dict[str, str] = {}
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) == 2:
                exporters[parts[1]] = parts[0]
        return exporters

    def netflow_query(
        self: _SupportsBase,
        start_time: str,
        end_time: str | None = None,
        exporter: str | None = None,
        filter: str | None = None,
        aggregate_by: list[str] | None = None,
        order_by: str = "bytes",
        limit: int = 25,
    ) -> list[dict]:
        """Return flow records, or per-key totals when ``aggregate_by`` is set."""
        keys = list(aggregate_by or [])
        unknown = [key for key in keys if key not in NETFLOW_AGGREGATE_KEYS]
        if unknown:
            raise ValueError(
                f"Unknown aggregate_by keys {unknown}; "
                f"use {sorted(NETFLOW_AGGREGATE_KEYS)}."
            )
        if order_by not in NETFLOW_ORDER_KEYS:
            raise ValueError(f"order_by must be one of {list(NETFLOW_ORDER_KEYS)}.")
        if filter is not None and len(filter) > NETFLOW_MAX_FILTER_CHARS:
            raise ValueError(
                f"filter is longer than {NETFLOW_MAX_FILTER_CHARS} characters."
            )

        exporters = self._flow_exporters()
        names_by_ip = {ip: name for name, ip in exporters.items()}
        clauses = [f"({filter})"] if filter else []
        if exporter is not None:
            if exporter not in exporters:
                raise ValueError(
                    f"Unknown exporter {exporter!r}; known: {sorted(exporters)}."
                )
            clauses.append(f"router ip {exporters[exporter]}")

        if keys:
            fields = [
                (NETFLOW_AGGREGATE_KEYS[k][1], NETFLOW_AGGREGATE_KEYS[k][2])
                for k in keys
            ]
            fields = [("%ts", "first_seen"), ("%te", "last_seen"), *fields]
        else:
            fields = list(_RECORD_FIELDS)
        fields.extend(_COUNTER_FIELDS)
        window = (
            _nfdump_time(start_time)
            + "-"
            + _nfdump_time(end_time if end_time is not None else str(time.time()))
        )
        argv = ["nfdump", "-R", FLOW_DIR, "-q", "-N", "-t", window]
        if keys:
            argv += ["-A", ",".join(NETFLOW_AGGREGATE_KEYS[k][0] for k in keys)]
        argv += ["-O", order_by, "-n", str(max(1, min(int(limit), NETFLOW_MAX_LIMIT)))]
        argv += ["-o", "fmt:" + "|".join(spec for spec, _ in fields)]
        if clauses:
            argv.append(" and ".join(clauses))

        raw = self.exec_cmd(FLOW_COLLECTOR, "TZ=UTC " + shlex.join(argv), timeout=30)
        rows: list[dict] = []
        unparsed: list[str] = []
        for line in raw.splitlines():
            values = [value.strip() for value in line.split("|")]
            if len(values) != len(fields):
                if line.strip() and line.strip() != "No matching flows":
                    unparsed.append(line.strip())
                continue
            row: dict = {}
            for (_, name), value in zip(fields, values, strict=True):
                if name == "exporter":
                    value = names_by_ip.get(value, value)
                elif name == "proto":
                    value = _PROTO_NAMES.get(value, value)
                elif name in _INT_FIELDS and value.isdigit():
                    value = int(value)
                row[name] = value
            rows.append(row)
        if not rows and unparsed:
            raise ValueError("nfdump failed: " + " ".join(unparsed)[:500])
        return rows


class KatharaNetflowAPI(KatharaBaseAPI, NetflowAPIMixin):
    """Kathara API for IPFIX flow records."""

    pass
