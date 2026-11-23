"""Read-only, lazily cached access to an offline-recording dataset folder."""

from __future__ import annotations

import csv
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path
from typing import Any, Iterable

SERIES_RE = re.compile(r"^(?P<device>[^{]+?)(?:\{(?P<labels>.*)\})?$")
INTERFACE_COUNTERS = {
    "rx_bytes": ("node_network_receive_bytes_total", "sonic_interface_in_octets"),
    "tx_bytes": ("node_network_transmit_bytes_total", "sonic_interface_out_octets"),
    "rx_packets": ("node_network_receive_packets_total", "sonic_interface_in_packets"),
    "tx_packets": (
        "node_network_transmit_packets_total",
        "sonic_interface_out_packets",
    ),
    "rx_errs": ("node_network_receive_errs_total", "sonic_interface_in_errors"),
    "tx_errs": ("node_network_transmit_errs_total", "sonic_interface_out_errors"),
    "rx_drop": ("node_network_receive_drop_total", "sonic_interface_in_discards"),
    "tx_drop": ("node_network_transmit_drop_total", "sonic_interface_out_discards"),
}
SONIC_METRICS = {
    name
    for pair in INTERFACE_COUNTERS.values()
    for name in pair
    if name.startswith("sonic_")
}


def parse_time_ms(value: str | None) -> int | None:
    """Parse an ISO-like timestamp (``Z``, offset, or naive UTC, ``T`` or space) to epoch ms."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def parse_series_name(name: str) -> tuple[str, dict[str, str]]:
    """Split ``dev{k=v,k2=v2}`` into the device name and its labels."""
    match = SERIES_RE.match(name.strip())
    if not match:
        return name, {}
    labels: dict[str, str] = {}
    for part in (match.group("labels") or "").split(","):
        if "=" in part:
            key, val = part.split("=", 1)
            labels[key.strip()] = val.strip().strip('"')
    return match.group("device"), labels


def _num(value: str) -> float | None:
    if value == "" or value is None:
        return None
    try:
        out = float(value)
    except ValueError:
        return None
    return out if math.isfinite(out) else None


def counter_rate(t: list[int], values: list[float | None]) -> list[float | None]:
    """Per-second rate of a monotonic counter; resets and gaps yield ``None``."""
    out: list[float | None] = [None] * len(values)
    prev_t, prev_v = None, None
    for i, (ts, v) in enumerate(zip(t, values)):
        if v is not None and prev_v is not None and ts > prev_t and v >= prev_v:
            out[i] = (v - prev_v) * 1000.0 / (ts - prev_t)
        if v is not None:
            prev_t, prev_v = ts, v
    return out


def is_counter(metric: str) -> bool:
    return (
        metric.endswith("_total")
        or metric in SONIC_METRICS
        or metric.startswith("p4_")
        or "_netstat_" in metric
    )


def sonic_port_name(port: str) -> str | None:
    """Map a front-panel ``ethN`` (N >= 1) to the SONiC ``Ethernet{4*(N-1)}`` alias."""
    match = re.fullmatch(r"eth(\d+)", port)
    if not match or int(match.group(1)) < 1:
        return None
    return f"Ethernet{4 * (int(match.group(1)) - 1)}"


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open() as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


class Task:
    """One task directory; heavy files are read on first use and cached."""

    def __init__(self, root: Path, task_id: str):
        self.root = root
        self.task_id = task_id
        self.dir = root / "tasks" / task_id
        self.before_dir = root / "before_fault" / task_id
        self._metric_index: list[dict[str, Any]] | None = None
        self._tables: dict[str, Any] = {}

    # ---- metadata -------------------------------------------------------
    @cached_property
    def meta(self) -> dict[str, Any]:
        return json.loads((self.dir / "task.json").read_text())

    @cached_property
    def timeline(self) -> dict[str, Any]:
        path = self.dir / "timeline.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        out = {
            k: data.get(k)
            for k in (
                "recording_start",
                "alert_time",
                "state_snapshot_time",
                "recording_end",
            )
        }
        for key in list(out):
            out[f"{key}_ms"] = parse_time_ms(out[key])
        return out

    def label(self) -> dict[str, Any] | None:
        path = self.root / "labels" / f"{self.task_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        data["fault_injected_at_ms"] = parse_time_ms(data.get("fault_injected_at"))
        return data

    def _views(self, base: Path, device: str) -> list[str]:
        folder = base / "devices" / device
        return sorted(p.stem for p in folder.glob("*.txt")) if folder.is_dir() else []

    @cached_property
    def segments(self) -> list[dict[str, Any]]:
        out = []
        for i, ends in enumerate(self.meta.get("segments", [])):
            out.append(
                {
                    "id": f"seg{i}",
                    "kind": "lan" if len(ends) > 2 else "link",
                    "ends": ends,
                }
            )
        return out

    @cached_property
    def collectors(self) -> dict[str, bool]:
        tel, probes = self.dir / "telemetry", self.dir / "probes"
        int_dir = tel / "int"
        return {
            "prometheus": any((tel / "prometheus").glob("*.csv")),
            "syslog": (tel / "syslog.log").exists(),
            "netflow": (tel / "netflow.csv").exists(),
            "int": int_dir.is_dir() and any(int_dir.glob("*.jsonl")),
            "pingmesh": (probes / "pingmesh.jsonl").exists(),
            "http_dns": (probes / "http_dns.jsonl").exists(),
            "iperf": (probes / "iperf.jsonl").exists(),
            "traceroute": (probes / "traceroute.json").exists(),
        }

    def controllers(self) -> list[str]:
        folder = self.dir / "controllers"
        return (
            sorted(p.name for p in folder.iterdir() if p.is_file())
            if folder.is_dir()
            else []
        )

    def detail(self) -> dict[str, Any]:
        metric_devices = {d for m in self.metrics() for d in m["devices"]}
        devices = [
            {
                **dev,
                "views": self._views(self.dir, dev["name"]),
                "before_fault_views": self._views(self.before_dir, dev["name"]),
                "has_metrics": dev["name"] in metric_devices,
            }
            for dev in self.meta.get("devices", [])
        ]
        return {
            "task_id": self.task_id,
            "network": self.meta.get("network"),
            "scenario": self.meta.get("scenario"),
            "topology_size": self.meta.get("topology_size"),
            "description": self.meta.get("description", ""),
            "probe_plan": self.meta.get("probe_plan", {}),
            "telemetry": self.meta.get("telemetry", []),
            "devices": devices,
            "segments": self.segments,
            "timeline": self.timeline,
            "collectors": self.collectors,
            "controllers": self.controllers(),
            "has_topology_png": (self.dir / "topology.png").is_file(),
            "netflow_exporters": sorted({r["exporter"] for r in self.netflow_rows}),
        }

    # ---- files ----------------------------------------------------------
    def view_text(self, device: str, view: str, phase: str = "snapshot") -> str | None:
        base = self.before_dir if phase == "before_fault" else self.dir
        path = _safe_child(base / "devices", device, f"{view}.txt")
        return path.read_text(errors="replace") if path and path.is_file() else None

    def controller_text(self, name: str) -> str | None:
        path = _safe_child(self.dir, "controllers", name)
        if not path or not path.is_file():
            return None
        text = path.read_text(errors="replace")
        if path.suffix == ".json":
            try:
                text = json.dumps(json.loads(text), indent=2)
            except json.JSONDecodeError:
                pass
        return text

    # ---- prometheus -----------------------------------------------------
    def _metric_path(self, metric: str) -> Path | None:
        return _safe_child(self.dir / "telemetry", "prometheus", f"{metric}.csv")

    def metrics(self) -> list[dict[str, Any]]:
        if self._metric_index is not None:
            return self._metric_index
        out = []
        for path in sorted((self.dir / "telemetry" / "prometheus").glob("*.csv")):
            with path.open(newline="") as handle:
                header = next(csv.reader(handle), [])
            devices = sorted({parse_series_name(col)[0] for col in header[1:]})
            out.append(
                {
                    "metric": path.stem,
                    "kind": "counter" if is_counter(path.stem) else "gauge",
                    "devices": devices,
                    "n_series": len(header) - 1,
                }
            )
        self._metric_index = out
        return out

    def _metric_table(
        self, metric: str
    ) -> tuple[list[int], list[str], list[list[float | None]]] | None:
        if metric not in self._tables:
            self._tables[metric] = self._load_metric_table(metric)
        return self._tables[metric]

    def _load_metric_table(
        self, metric: str
    ) -> tuple[list[int], list[str], list[list[float | None]]] | None:
        path = self._metric_path(metric)
        if not path or not path.is_file():
            return None
        with path.open(newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, [])
            columns: list[list[float | None]] = [[] for _ in header[1:]]
            times: list[int] = []
            for row in reader:
                ts = parse_time_ms(row[0]) if row else None
                if ts is None:
                    continue
                times.append(ts)
                for i in range(len(columns)):
                    columns[i].append(_num(row[i + 1]) if i + 1 < len(row) else None)
        return times, header[1:], columns

    def metric_series(
        self, metric: str, devices: list[str] | None = None, rate: str = "auto"
    ) -> dict[str, Any] | None:
        table = self._metric_table(metric)
        if table is None:
            return None
        times, names, columns = table
        kind = "counter" if is_counter(metric) else "gauge"
        apply_rate = rate == "true" or (rate == "auto" and kind == "counter")
        series = []
        for name, values in zip(names, columns):
            device, labels = parse_series_name(name)
            if devices and device not in devices:
                continue
            series.append(
                {
                    "id": name,
                    "device": device,
                    "labels": labels,
                    "values": counter_rate(times, values) if apply_rate else values,
                }
            )
        return {
            "metric": metric,
            "kind": kind,
            "rate": apply_rate,
            "t": times,
            "series": series,
        }

    def interface(self, device: str, port: str) -> dict[str, Any]:
        """Per-second interface counters for one device port, from node_exporter or SONiC metrics."""
        candidates = [("node_network", port), ("sonic", sonic_port_name(port))]
        for source, label in candidates:
            if label is None:
                continue
            counters: dict[str, list[float | None]] = {}
            times: list[int] = []
            column = f"{device}{{device={label}}}"
            for key, (node_metric, sonic_metric) in INTERFACE_COUNTERS.items():
                table = self._metric_table(
                    node_metric if source == "node_network" else sonic_metric
                )
                if table is None:
                    continue
                t, names, cols = table
                for name, values in zip(names, cols):
                    dev, labels = parse_series_name(name)
                    if dev == device and labels.get("device") == label:
                        counters[key] = counter_rate(t, values)
                        times = times or t
                        break
            if counters:
                return {
                    "device": device,
                    "port": port,
                    "source": source,
                    "column": column,
                    "t": times,
                    "counters": counters,
                }
        return {
            "device": device,
            "port": port,
            "source": None,
            "column": None,
            "t": [],
            "counters": {},
        }

    # ---- syslog ---------------------------------------------------------
    @cached_property
    def syslog_rows(self) -> list[dict[str, Any]]:
        path = self.dir / "telemetry" / "syslog.log"
        rows = []
        if not path.exists():
            return rows
        with path.open(errors="replace") as handle:
            for line in handle:
                line = line.rstrip("\n")
                if not line:
                    continue
                parts = line.split(" ", 2)
                if len(parts) < 3:
                    continue
                ts, device, rest = parts
                program, _, message = rest.partition(": ")
                if not _:
                    program, message = "", rest
                rows.append(
                    {
                        "t": parse_time_ms(ts),
                        "device": device,
                        "program": program,
                        "message": message,
                    }
                )
        rows.sort(key=lambda r: r["t"] or 0)
        return rows

    # ---- netflow --------------------------------------------------------
    @cached_property
    def netflow_rows(self) -> list[dict[str, Any]]:
        path = self.dir / "telemetry" / "netflow.csv"
        rows = []
        if not path.exists():
            return rows
        with path.open(newline="") as handle:
            for rec in csv.DictReader(handle):
                rows.append(
                    {
                        "first_ms": parse_time_ms(rec.get("first_seen")),
                        "last_ms": parse_time_ms(rec.get("last_seen")),
                        "exporter": rec.get("exporter", ""),
                        "in_if": rec.get("in_if", ""),
                        "proto": rec.get("proto", ""),
                        "src_ip": rec.get("src_ip", ""),
                        "src_port": _int(rec.get("src_port")),
                        "dst_ip": rec.get("dst_ip", ""),
                        "dst_port": _int(rec.get("dst_port")),
                        "packets": _int(rec.get("packets")),
                        "bytes": _int(rec.get("bytes")),
                        "flows": _int(rec.get("flows")),
                    }
                )
        rows.sort(key=lambda r: r["first_ms"] or 0)
        return rows

    def netflow(
        self,
        exporters: list[str] | None,
        q: str | None,
        start: int | None,
        end: int | None,
        limit: int,
    ) -> dict[str, Any]:
        rows = self.netflow_rows
        matched = [
            r
            for r in rows
            if (not exporters or r["exporter"] in exporters)
            and (start is None or (r["last_ms"] or 0) >= start)
            and (end is None or (r["first_ms"] or 0) <= end)
            and (
                not q
                or q
                in f"{r['src_ip']}:{r['src_port']} {r['dst_ip']}:{r['dst_port']} {r['proto']} {r['exporter']}"
            )
        ]
        talkers: dict[tuple, dict[str, Any]] = {}
        by_exp: dict[str, dict[str, Any]] = {}
        for r in matched:
            key = (r["src_ip"], r["dst_ip"], r["proto"], r["dst_port"])
            agg = talkers.setdefault(
                key,
                {
                    "src_ip": key[0],
                    "dst_ip": key[1],
                    "proto": key[2],
                    "dst_port": key[3],
                    "bytes": 0,
                    "packets": 0,
                    "flows": 0,
                },
            )
            exp = by_exp.setdefault(
                r["exporter"],
                {"exporter": r["exporter"], "bytes": 0, "packets": 0, "flows": 0},
            )
            for target in (agg, exp):
                target["bytes"] += r["bytes"]
                target["packets"] += r["packets"]
                target["flows"] += r["flows"]
        top = sorted(talkers.values(), key=lambda x: -x["bytes"])[:25]
        return {
            "total": len(rows),
            "matched": len(matched),
            "rows": matched[:limit],
            "top_talkers": top,
            "by_exporter": sorted(by_exp.values(), key=lambda x: -x["bytes"]),
            "timeseries": self._netflow_timeseries(matched),
        }

    def _netflow_timeseries(
        self, rows: list[dict[str, Any]], bucket_s: int = 10
    ) -> dict[str, Any]:
        """Spread each flow record's bytes evenly over its active interval, in fixed buckets."""
        start, end = (
            self.timeline.get("recording_start_ms"),
            self.timeline.get("recording_end_ms"),
        )
        if not rows or start is None or end is None:
            return {"t": [], "bucket_s": bucket_s, "series": []}
        width = bucket_s * 1000
        n = max(1, math.ceil((end - start) / width))
        series: dict[str, list[float]] = defaultdict(lambda: [0.0] * n)
        for r in rows:
            first, last = r["first_ms"] or start, r["last_ms"] or r["first_ms"] or start
            last = max(last, first + 1)
            span = last - first
            b0, b1 = (
                max(0, (first - start) // width),
                min(n - 1, (last - start) // width),
            )
            for b in range(int(b0), int(b1) + 1):
                lo, hi = (
                    max(first, start + b * width),
                    min(last, start + (b + 1) * width),
                )
                if hi > lo or span == 1:
                    series[r["exporter"]][b] += (
                        r["bytes"] * (max(hi - lo, 1) / span) / bucket_s
                    )
        return {
            "t": [start + i * width for i in range(n)],
            "bucket_s": bucket_s,
            "series": [
                {"exporter": k, "values": [round(v, 2) for v in vals]}
                for k, vals in sorted(series.items())
            ],
        }

    # ---- probes ---------------------------------------------------------
    def pingmesh(self) -> dict[str, Any]:
        path = self.dir / "probes" / "pingmesh.jsonl"
        rounds = list(_read_jsonl(path)) if path.exists() else []
        rounds.sort(key=lambda r: parse_time_ms(r.get("time")) or 0)
        t = [parse_time_ms(r.get("time")) for r in rounds]
        pairs: dict[tuple[str, str], dict[str, list]] = {}
        endpoints: dict[str, str] = {}
        summary = []
        for i, rnd in enumerate(rounds):
            endpoints.update(rnd.get("endpoints") or {})
            s = rnd.get("summary") or {}
            summary.append(
                {
                    **{
                        k: s.get(k)
                        for k in (
                            "reachable_pairs",
                            "total_pairs",
                            "anomaly_count",
                            "unreachable",
                            "packet_loss",
                            "high_latency",
                            "policy_violation",
                        )
                    },
                    "rtt_median": (s.get("rtt_ms") or {}).get("median"),
                }
            )
            for res in rnd.get("results") or []:
                key = (res.get("source"), res.get("target"))
                entry = pairs.setdefault(
                    key,
                    {
                        "status": [None] * len(rounds),
                        "loss": [None] * len(rounds),
                        "rtt": [None] * len(rounds),
                        "expected": [None] * len(rounds),
                    },
                )
                entry["status"][i] = res.get("status")
                entry["loss"][i] = res.get("loss_percent")
                entry["rtt"][i] = res.get("rtt_avg_ms")
                entry["expected"][i] = res.get("expected")
        return {
            "t": t,
            "summary": summary,
            "pairs": [
                {"source": s, "target": d, **v} for (s, d), v in sorted(pairs.items())
            ],
            "endpoints": endpoints,
        }

    def http_dns(self) -> dict[str, Any]:
        path = self.dir / "probes" / "http_dns.jsonl"
        rows = []
        for rec in _read_jsonl(path) if path.exists() else []:
            kind = rec.get("kind")
            if kind == "http":
                code = str(rec.get("code", ""))
                total = _num(str(rec.get("total", "")))
                rows.append(
                    {
                        "t": parse_time_ms(rec.get("time")),
                        "kind": "http",
                        "source": rec.get("source"),
                        "target": rec.get("target"),
                        "ok": code.startswith(("2", "3")),
                        "code": code,
                        "latency_ms": None if total is None else round(total * 1000, 2),
                        "detail": {
                            k: v
                            for k, v in rec.items()
                            if k
                            not in {"time", "kind", "source", "target", "code", "raw"}
                        },
                    }
                )
            elif kind == "dns":
                status = rec.get("status", "")
                rows.append(
                    {
                        "t": parse_time_ms(rec.get("time")),
                        "kind": "dns",
                        "source": rec.get("source"),
                        "target": rec.get("name"),
                        "ok": status == "NOERROR" and bool(rec.get("answers")),
                        "code": status,
                        "latency_ms": rec.get("query_ms"),
                        "detail": {
                            k: v
                            for k, v in rec.items()
                            if k
                            not in {
                                "time",
                                "kind",
                                "source",
                                "name",
                                "status",
                                "query_ms",
                            }
                        },
                    }
                )
        rows.sort(key=lambda r: r["t"] or 0)
        return {"rows": rows}

    def iperf(self) -> dict[str, Any]:
        path = self.dir / "probes" / "iperf.jsonl"
        rows = []
        for rec in _read_jsonl(path) if path.exists() else []:
            rows.append(
                {
                    "t": parse_time_ms(rec.get("time")),
                    "source": rec.get("source"),
                    "target": rec.get("target"),
                    "sender_mbps": rec.get("sender_mbps"),
                    "receiver_mbps": rec.get("receiver_mbps"),
                    "retransmits": rec.get("retransmits"),
                    "ok": bool(rec.get("receiver_mbps")),
                }
            )
        rows.sort(key=lambda r: r["t"] or 0)
        return {"rows": rows}

    def traceroute(self) -> list[dict[str, Any]]:
        path = self.dir / "probes" / "traceroute.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text())
        return data if isinstance(data, list) else []

    # ---- INT ------------------------------------------------------------
    def _int_files(self) -> list[Path]:
        return sorted((self.dir / "telemetry" / "int").glob("*.jsonl"))

    @cached_property
    def int_summary(self) -> dict[str, Any]:
        """Stream the INT reports once and aggregate per switch and second."""
        files = self._int_files()
        if not files:
            return {
                "available": False,
                "n_reports": 0,
                "t": [],
                "switches": [],
                "top_flows": [],
            }
        buckets: dict[int, dict[int, list[float]]] = defaultdict(
            dict
        )  # sec -> switch -> [count, lat_sum, lat_max, q_max]
        flows: dict[tuple, dict[str, Any]] = {}
        n = 0
        for path in files:
            for rec in _read_jsonl(path):
                n += 1
                ts = rec.get("packet_timestamp")
                if not isinstance(ts, (int, float)):
                    continue
                sec = int(ts // 1_000_000_000)
                hops = rec.get("hop_sequence") or []
                for hop in hops:
                    sid = hop.get("switch_id")
                    lat_us = (hop.get("hop_latency") or 0) / 1000.0
                    q = hop.get("queue_occupancy") or 0
                    acc = buckets[sec].setdefault(sid, [0, 0.0, 0.0, 0])
                    acc[0] += 1
                    acc[1] += lat_us
                    acc[2] = max(acc[2], lat_us)
                    acc[3] = max(acc[3], q)
                key = (
                    rec.get("src"),
                    rec.get("dst"),
                    rec.get("protocol"),
                    rec.get("dst_port"),
                )
                flow = flows.setdefault(
                    key,
                    {
                        "src": key[0],
                        "dst": key[1],
                        "protocol": key[2],
                        "dst_port": key[3],
                        "reports": 0,
                        "hops": "",
                    },
                )
                flow["reports"] += 1
                flow["hops"] = " → ".join(str(h.get("switch_id")) for h in hops)
        secs = sorted(buckets)
        if secs:
            secs = list(range(secs[0], secs[-1] + 1))
        switch_ids = sorted(
            {sid for b in buckets.values() for sid in b}, key=lambda x: (x is None, x)
        )
        switches = []
        for sid in switch_ids:
            accs = [buckets.get(s, {}).get(sid) for s in secs]
            switches.append(
                {
                    "switch_id": sid,
                    "reports": [a[0] if a else 0 for a in accs],
                    "hop_latency_avg_us": [
                        round(a[1] / a[0], 2) if a else None for a in accs
                    ],
                    "hop_latency_max_us": [round(a[2], 2) if a else None for a in accs],
                    "queue_max": [a[3] if a else None for a in accs],
                }
            )
        return {
            "available": True,
            "n_reports": n,
            "t": [s * 1000 for s in secs],
            "switches": switches,
            "top_flows": sorted(flows.values(), key=lambda f: -f["reports"])[:25],
        }

    def int_records(self, offset: int, limit: int) -> dict[str, Any]:
        rows, total = [], 0
        for path in self._int_files():
            with path.open() as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    if offset <= total < offset + limit:
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
                    total += 1
        return {"total": total, "rows": rows}


def _int(value: str | None) -> int:
    try:
        return int(float(value)) if value not in (None, "") else 0
    except ValueError:
        return 0


def _safe_child(base: Path, *parts: str) -> Path | None:
    """Join path parts below ``base``, rejecting traversal."""
    for part in parts:
        if not part or "/" in part or "\\" in part or part in {".", ".."}:
            return None
    return base.joinpath(*parts)


class Dataset:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self._tasks: dict[str, Task] = {}

    def task_ids(self) -> list[str]:
        folder = self.root / "tasks"
        return (
            sorted(p.name for p in folder.iterdir() if (p / "task.json").exists())
            if folder.is_dir()
            else []
        )

    def task(self, task_id: str) -> Task | None:
        if _safe_child(self.root, task_id) is None or task_id not in self.task_ids():
            return None
        if task_id not in self._tasks:
            self._tasks[task_id] = Task(self.root, task_id)
        return self._tasks[task_id]

    def list_tasks(self) -> list[dict[str, Any]]:
        out = []
        for tid in self.task_ids():
            task = self.task(tid)
            meta, tl = task.meta, task.timeline
            out.append(
                {
                    "task_id": tid,
                    "network": meta.get("network"),
                    "topology_size": meta.get("topology_size"),
                    "n_devices": len(meta.get("devices", [])),
                    "n_segments": len(meta.get("segments", [])),
                    "telemetry": meta.get("telemetry", []),
                    "recording_start": tl.get("recording_start"),
                    "alert_time": tl.get("alert_time"),
                    "recording_end": tl.get("recording_end"),
                    "has_label": (self.root / "labels" / f"{tid}.json").exists(),
                }
            )
        return out
