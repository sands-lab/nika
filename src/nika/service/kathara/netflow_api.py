import json
import re
import shlex
import time
from datetime import UTC, datetime

from nika.service.kathara.base_api import KatharaBaseAPI, _SupportsBase
from nika.service.kathara.telemetry_api import time_bound_seconds
from nika.telemetry.management import TELEMETRY_COLLECTOR
from nika.telemetry.netflow import FLOW_DIR, FLOW_EXPORTERS_FILE

# Agent-facing aggregation keys mapped to nfdump ``-A`` keys and output fields.
NETFLOW_AGGREGATE_KEYS: dict[str, tuple[str, str, str]] = {
    "router": ("router", "%ra", "exporter"),
    "inif": ("inif", "%in", "in_if"),
    "proto": ("proto", "%pr", "proto"),
    "tos": ("tos", "%tos", "tos"),
    "outif": ("outif", "%out", "out_if"),
    "nexthop": ("nexthop", "%nh", "next_hop"),
    "srcas": ("srcas", "%sas", "src_as"),
    "dstas": ("dstas", "%das", "dst_as"),
    "srcip": ("srcip", "%sa", "src_ip"),
    "srcport": ("srcport", "%sp", "src_port"),
    "dstip": ("dstip", "%da", "dst_ip"),
    "dstport": ("dstport", "%dp", "dst_port"),
}
NETFLOW_ORDER_KEYS = (
    "bytes",
    "packets",
    "flows",
    "pps",
    "bps",
    "bpp",
    "tstart",
    "tend",
    "duration",
)
MAX_ROWS = 1000
MAX_OUTPUT = 100_000
NETFLOW_MAX_FILTER_CHARS = 512

_RATE_FIELDS = (
    ("%td", "duration_seconds"),
    ("%pps", "pps"),
    ("%bps", "bps"),
    ("%bpp", "bytes_per_packet"),
)
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
    ("%tos", "tos"),
    ("%flg", "tcp_flags"),
    *_RATE_FIELDS,
)
_COUNTER_FIELDS = (("%pkt", "packets"), ("%byt", "bytes"), ("%fl", "flows"))
_INT_FIELDS = {
    "in_if",
    "out_if",
    "src_as",
    "dst_as",
    "tos",
    "src_port",
    "dst_port",
    "packets",
    "bytes",
    "flows",
    "reverse_packets",
    "reverse_bytes",
}
_PROTO_NAMES = {
    "1": "icmp",
    "2": "igmp",
    "6": "tcp",
    "17": "udp",
    "47": "gre",
    "89": "ospf",
}


def _nfdump_time(value: str) -> str:
    """Format a time bound (see ``time_bound_seconds``) as an nfdump ``-t`` timestamp."""
    seconds = time_bound_seconds(value)
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y/%m/%d.%H:%M:%S")


class NetflowAPIMixin:
    """Query IPFIX flow records stored by the lab telemetry collector."""

    def _flow_exporters(self: _SupportsBase) -> dict[str, str]:
        raw = self.exec_cmd(TELEMETRY_COLLECTOR, f"cat {FLOW_EXPORTERS_FILE}")
        exporters: dict[str, str] = {}
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) == 2:
                exporters[parts[1]] = parts[0]
        return exporters

    def _flow_query(
        self: _SupportsBase,
        start_time: str,
        end_time: str | None = None,
        exporter: str | None = None,
        filter: str | None = None,
        aggregate_by: list[str] | None = None,
        order_by: str = "bytes",
        limit: int = 25,
        conversations: bool = False,
    ) -> dict:
        """Return flow records, or per-key totals when ``aggregate_by`` is set."""
        keys = list(aggregate_by or [])
        key_specs = dict(NETFLOW_AGGREGATE_KEYS)
        for key in keys:
            match = re.fullmatch(r"(src|dst)ip([46])/(\d{1,3})", key)
            if match and int(match[3]) <= (32 if match[2] == "4" else 128):
                key_specs[key] = (
                    key,
                    "%sa" if match[1] == "src" else "%da",
                    match[1] + "_ip",
                )
        unknown = [key for key in keys if key not in key_specs]
        if unknown:
            raise ValueError(
                f"Unknown aggregate_by keys {unknown}; "
                f"use {sorted(NETFLOW_AGGREGATE_KEYS)}."
            )
        if int(limit) < 1:
            raise ValueError("limit must be at least 1.")
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
            fields = [(key_specs[k][1], key_specs[k][2]) for k in keys]
            fields = [
                ("%ts", "first_seen"),
                ("%te", "last_seen"),
                *fields,
                *_RATE_FIELDS,
            ]
        else:
            fields = list(_RECORD_FIELDS)
        fields.extend(_COUNTER_FIELDS)
        if conversations:
            fields.extend([("%opkt", "reverse_packets"), ("%obyt", "reverse_bytes")])
        window = (
            _nfdump_time(start_time)
            + "-"
            + _nfdump_time(end_time if end_time is not None else str(time.time()))
        )
        argv = [
            "nfdump",
            "-C",
            "none",
            "-G",
            "none",
            "-R",
            FLOW_DIR,
            "-q",
            "-N",
            "-t",
            window,
        ]
        if conversations:
            argv.append("-B")
        if keys:
            argv += ["-A", ",".join(key_specs[k][0] for k in keys)]
        argv += ["-O", order_by, "-n", str(min(int(limit), MAX_ROWS))]
        argv += ["-o", "fmt:" + "|".join(spec for spec, _ in fields)]
        if clauses:
            argv.append(" and ".join(clauses))

        command = bounded_command(argv)
        raw = self.exec_cmd(TELEMETRY_COLLECTOR, command, timeout=30)
        if "timeout: sending signal" in raw:
            raise TimeoutError("The nfdump query exceeded 25 seconds.")
        inventory = json.loads(
            self.exec_cmd(TELEMETRY_COLLECTOR, "cat /var/lib/flow_inventory.json")
        )
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
                elif name in {"duration_seconds", "pps", "bps", "bytes_per_packet"}:
                    try:
                        value = float(value)
                    except ValueError:
                        pass
                row[name] = value
            iface = (
                inventory.get(row.get("exporter"), {})
                .get("interfaces", {})
                .get(str(row.get("in_if")), {})
            )
            if "in_if" in row:
                row["in_if_name"] = iface.get("name")
                row["link_peers"] = iface.get("link_peers", [])
            if isinstance(row.get("tos"), int):
                row["dscp"] = row["tos"] >> 2
            rows.append(row)
        if not rows and unparsed:
            raise ValueError("nfdump failed: " + " ".join(unparsed)[:500])
        return {
            "command": command,
            "rows": rows[: min(int(limit), MAX_ROWS)],
            "truncated": len(raw) >= MAX_OUTPUT,
        }

    def flow_records(
        self,
        start_time: str = "-600",
        end_time: str | None = None,
        exporter: str | None = None,
        filter: str | None = None,
        order_by: str = "tstart",
        limit: int = 25,
    ) -> dict:
        return self._flow_query(
            start_time, end_time, exporter, filter, order_by=order_by, limit=limit
        )

    def flow_top_talkers(
        self,
        aggregate_by: list[str],
        start_time: str = "-600",
        end_time: str | None = None,
        exporter: str | None = None,
        filter: str | None = None,
        order_by: str = "bytes",
        limit: int = 25,
    ) -> dict:
        if not aggregate_by:
            raise ValueError("aggregate_by must contain at least one key.")
        # Preserve node/interface context and avoid merging observations from different hops.
        keys = list(dict.fromkeys(["router", *aggregate_by]))
        return self._flow_query(
            start_time, end_time, exporter, filter, keys, order_by, limit
        )

    def flow_conversations(
        self,
        start_time: str = "-600",
        end_time: str | None = None,
        exporter: str | None = None,
        filter: str | None = None,
        limit: int = 25,
    ) -> dict:
        if exporter is None:
            raise ValueError(
                "Select one exporter to avoid merging observations across hops."
            )
        return self._flow_query(
            start_time, end_time, exporter, filter, limit=limit, conversations=True
        )

    def flow_coverage(self, limit: int = 25, offset: int = 0) -> dict:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset nonnegative.")
        command = "curl -fsS --max-time 5 http://127.0.0.1:9102/coverage"
        inventory = json.loads(self.exec_cmd(TELEMETRY_COLLECTOR, command, timeout=10))
        rows = [
            {
                "node": node,
                **{
                    key: info[key]
                    for key in ("supported", "newest_record", "last_update")
                },
            }
            for node, info in sorted(inventory.items())
        ]
        stop = offset + min(limit, MAX_ROWS)
        return {
            "command": command,
            "rows": rows[offset:stop],
            "next_offset": stop if stop < len(rows) else None,
            "total_nodes": len(rows),
        }

    def nfdump(self, args: list[str]) -> dict:
        argv = restricted_args(args)
        command = bounded_command(argv)
        raw = self.exec_cmd(TELEMETRY_COLLECTOR, command, timeout=30)
        return {
            "command": command,
            "text": "\n".join(raw[:MAX_OUTPUT].splitlines()[:MAX_ROWS]),
            "truncated": len(raw) > MAX_OUTPUT or len(raw.splitlines()) > MAX_ROWS,
            "exporters": self._flow_exporters(),
        }

    def flow_timeline(
        self,
        start_time: str = "-600",
        end_time: str | None = None,
        metric: str = "bytes",
        split_by: str | None = None,
        step: str = "15s",
        exporter: str | None = None,
        limit: int = 50,
    ) -> dict:
        from nika.service.kathara.prometheus_api import prometheus_query_for

        expression = rate_query(metric, split_by, exporter, "1m")
        return {
            "query": expression,
            "rows": prometheus_query_for(
                self,
                query=expression,
                start_time=start_time,
                end_time=end_time,
                step=step,
                limit=min(limit, MAX_ROWS),
            ),
        }

    def flow_current_load(
        self,
        metric: str = "bytes",
        exporter: str | None = None,
        average_window: str = "5m",
        limit: int = 50,
    ) -> dict:
        from nika.service.kathara.prometheus_api import prometheus_query_for

        if not re.fullmatch(r"[1-9]\d{0,3}[smh]", average_window):
            raise ValueError("average_window must be a duration such as 5m.")
        current = rate_query(metric, None, exporter, "1m", latest=True)
        average = rate_query(metric, None, exporter, average_window)
        queries = {
            "current": current,
            "average": average,
            "ratio": f"({current}) / ({average})",
        }
        values = {
            key: {
                row["node"]: row["value"]
                for row in prometheus_query_for(
                    self, query=value, limit=min(limit, MAX_ROWS)
                )
            }
            for key, value in queries.items()
        }
        nodes = sorted({node for series in values.values() for node in series})
        return {
            "queries": queries,
            "rows": [
                {
                    "node": node,
                    **{key: series.get(node) for key, series in values.items()},
                }
                for node in nodes[: min(limit, MAX_ROWS)]
            ],
        }


class _ExecNetflow(NetflowAPIMixin):
    """Bind ``NetflowAPIMixin`` to any lab API that implements ``exec_cmd``."""

    def __init__(self, api) -> None:
        self._api = api

    def exec_cmd(self, host_name: str, command: str, timeout: float = 10) -> str:
        return self._api.exec_cmd(host_name, command, timeout=timeout)


def flow_api_for(api) -> NetflowAPIMixin:
    """Bind flow tools to either runtime's execution API."""
    return _ExecNetflow(api)


class KatharaNetflowAPI(KatharaBaseAPI, NetflowAPIMixin):
    """Kathara API for IPFIX flow records."""

    pass


def bounded_command(argv: list[str]) -> str:
    return (
        "TZ=UTC timeout --verbose -s KILL 25s "
        + shlex.join(argv)
        + f" 2>&1 | head -c {MAX_OUTPUT + 1}"
    )


def restricted_args(args: list[str]) -> list[str]:
    """Allow query options only. Never accept filenames, config files, or writes."""
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError("args must be an argument list.")
    if sum(map(len, args)) > 4096:
        raise ValueError("The argument list exceeds 4096 characters.")
    flags = {"-a", "-b", "-B", "-6", "-q", "-N", "-I"}
    values = {"-A", "-O", "-t", "-o", "-s", "-n", "-c"}
    accepted = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in flags:
            accepted.append(arg)
        elif arg in values:
            i += 1
            if i >= len(args):
                raise ValueError(f"{arg} requires a value.")
            value = args[i]
            if arg in {"-n", "-c"}:
                value = str(max(1, min(int(value), MAX_ROWS)))
            elif value.startswith("-"):
                raise ValueError(f"Invalid value for {arg}.")
            accepted.extend([arg, value])
        elif not arg.startswith("-") and i == len(args) - 1:
            if len(arg) > NETFLOW_MAX_FILTER_CHARS:
                raise ValueError("The filter exceeds 512 characters.")
            accepted.append(arg)
        else:
            raise ValueError(
                f"Option {arg!r} is not permitted. Use query options only."
            )
        i += 1
    # nfdump may auto-select an event-oriented format for ordinary flow records.
    # Select its flow format unless the caller requested a format or file summary.
    if "-o" in args or "-I" in args:
        output = []
    elif "-B" in args:
        output = ["-o", "fmt:%ts|%te|%pr|%sa|%sp|%da|%dp|%pkt|%byt|%opkt|%obyt"]
    else:
        output = ["-o", "line"]
    return [
        "nfdump",
        "-C",
        "none",
        "-G",
        "none",
        "-R",
        FLOW_DIR,
        "-q",
        "-N",
        *output,
        *accepted,
    ]


def rate_query(
    metric: str,
    split_by: str | None,
    exporter: str | None,
    window: str,
    *,
    latest: bool = False,
) -> str:
    if metric not in {"bytes", "packets", "flows"}:
        raise ValueError("metric must be bytes, packets, or flows.")
    if split_by not in {None, "proto", "in_if"}:
        raise ValueError("split_by must be proto or in_if.")
    selector = "{node=" + json.dumps(exporter) + "}" if exporter else ""
    labels = "node" + ("," + split_by if split_by else "")
    function = "irate" if latest else "rate"
    return f"sum by ({labels}) ({function}(netflow_{metric}_total{selector}[{window}]))"
