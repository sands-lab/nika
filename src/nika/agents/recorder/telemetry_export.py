"""Export the session's collected telemetry to plain files.

Everything is read through ``exec_cmd`` on the lab's telemetry collector, the
same path the agent-facing MCP tools use. Collector internals and NIKA
identifiers are dropped with the filters those tools apply.
"""

from __future__ import annotations

import csv
import json
import re
import shlex
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nika.service.kathara.netflow_api import netflow_query_for
from nika.telemetry.management import TELEMETRY_COLLECTOR
from nika.telemetry.syslog import SYSLOG_DIR, SYSLOG_HOSTS, parse_log_line

PROM_URL = "http://127.0.0.1:9090"
# Labels that describe NIKA's scrape plumbing rather than the network.
_DROP_LABELS = {"__name__", "instance", "job"}
INT_REPORTS = "/var/lib/int-collector/int_reports.jsonl"
MAX_FLOWS = 20000
MAX_INT_LINES = 20000


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def _prom_get(api: Any, path: str, params: dict[str, str], timeout: float = 60) -> Any:
    args = " ".join(
        f"--data-urlencode {shlex.quote(f'{k}={v}')}" for k, v in params.items()
    )
    raw = api.exec_cmd(
        TELEMETRY_COLLECTOR,
        f"curl -sS -G --max-time {int(timeout) - 5} {args} {PROM_URL}{path}",
        timeout=timeout,
    )
    body = json.loads(raw)
    if body.get("status") != "success":
        raise RuntimeError(f"prometheus {path}: {raw[:300]}")
    return body["data"]


def _series_key(labels: dict[str, str]) -> str:
    keep = {k: v for k, v in labels.items() if k not in _DROP_LABELS}
    node = keep.pop("node", None)
    rest = ",".join(f"{k}={v}" for k, v in sorted(keep.items()))
    return f"{node}{'{' + rest + '}' if rest else ''}" if node else rest or "value"


def export_prometheus(api: Any, out: Path, start: float, end: float, step: str) -> dict:
    """One CSV per metric: a ``time`` column, then one column per series."""
    out.mkdir(parents=True, exist_ok=True)
    names = sorted(_prom_get(api, "/api/v1/label/__name__/values", {}))
    # Scrape plumbing, not network state.
    names = [
        n
        for n in names
        if not n.startswith(
            (
                "prometheus_",
                "scrape_",
                "up",
                "go_",
                "process_",
                "promhttp_",
                "node_scrape_",
                "node_textfile_",
                "node_exporter_",
            )
        )
    ]
    stats = {"metrics": 0, "series": 0, "errors": []}
    for name in names:
        try:
            data = _prom_get(
                api,
                "/api/v1/query_range",
                {
                    "query": name,
                    "start": f"{start:.3f}",
                    "end": f"{end:.3f}",
                    "step": step,
                },
            )
        except Exception as exc:  # noqa: BLE001 - keep the other metrics
            stats["errors"].append(f"{name}: {exc}"[:300])
            continue
        columns: dict[str, dict[float, str]] = {}
        for series in data.get("result", []):
            key = _series_key(series.get("metric", {}))
            columns.setdefault(key, {}).update(
                {float(t): v for t, v in series["values"]}
            )
        if not columns:
            continue
        times = sorted({t for col in columns.values() for t in col})
        keys = sorted(columns)
        with (out / f"{name}.csv").open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["time", *keys])
            for t in times:
                writer.writerow([_iso(t), *(columns[k].get(t, "") for k in keys)])
        stats["metrics"] += 1
        stats["series"] += len(keys)
    return stats


def export_syslog(api: Any, out: Path, start: float, end: float) -> dict:
    raw_hosts = api.exec_cmd(TELEMETRY_COLLECTOR, f"cat {SYSLOG_HOSTS}")
    names = {}
    for line in raw_hosts.splitlines():
        parts = line.split()
        if len(parts) == 2:
            names[parts[0]] = parts[1]
    raw = api.exec_cmd(
        TELEMETRY_COLLECTOR, f"cat {SYSLOG_DIR}/*.log 2>/dev/null", timeout=60
    )
    rows = []
    for line in raw.splitlines():
        parsed = parse_log_line(line)
        if parsed is None:
            continue
        try:
            ts = datetime.fromisoformat(
                parsed["time"].replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            continue
        if not start <= ts <= end:
            continue
        host = names.get(parsed["host"], parsed["host"])
        rows.append((ts, f"{_iso(ts)} {host} {parsed['program']}: {parsed['message']}"))
    rows.sort()
    out.write_text("".join(r[1] + "\n" for r in rows))
    return {"lines": len(rows)}


def export_netflow(api: Any, out: Path, start: float, end: float) -> dict:
    rows = netflow_query_for(
        api,
        start_time=str(start),
        end_time=str(end),
        order_by="bytes",
        limit=MAX_FLOWS,
    )
    rows.sort(key=lambda r: str(r.get("first_seen", "")))
    if rows:
        with out.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    else:
        out.write_text("(no flow records: no device in this network exports flows)\n")
    return {"flows": len(rows), "truncated": len(rows) >= MAX_FLOWS}


def export_int(api: Any, nodes: list[str], out: Path, start: float, end: float) -> dict:
    stats = {}
    for node in nodes:
        raw = api.exec_cmd(
            node,
            f"test -f {INT_REPORTS} && tail -n {MAX_INT_LINES} {INT_REPORTS}",
            timeout=60,
        )
        lines = [ln for ln in raw.splitlines() if ln.startswith("{")]
        if not lines:
            continue
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{node}_int_reports.jsonl").write_text("\n".join(lines) + "\n")
        stats[node] = len(lines)
    return stats


def telemetry_kinds(meta: dict) -> list[str]:
    kinds = meta.get("telemetry") or (meta.get("scenario_params") or {}).get(
        "telemetry"
    )
    if isinstance(kinds, str):
        kinds = re.split(r"[,\s]+", kinds)
    return sorted(k for k in (kinds or []) if k)
