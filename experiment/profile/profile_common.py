"""Shared flattening and aggregation of per-session ``resources.json`` summaries."""

from __future__ import annotations

import csv
import statistics
from pathlib import Path
from typing import Any, Iterable

from profile_resources import OTHER_PHASE, PHASES

# ``other`` keeps time no lifecycle event covers visible (e.g. the hold).
REPORTED_PHASES = tuple(spec.name for spec in PHASES) + (OTHER_PHASE,)
PHASE_METRICS = (
    "seconds",
    "cpu_seconds",
    "peak_memory_bytes",
    "peak_working_set_bytes",
    "io_read_bytes",
    "io_write_bytes",
    "peak_pids",
    "framework_cpu_seconds",
)
TOTAL_METRICS = (
    "cpu_seconds",
    "peak_cpu_percent",
    "peak_memory_bytes",
    "peak_working_set_bytes",
    "io_read_bytes",
    "io_write_bytes",
    "peak_pids",
    "peak_containers",
    "framework_cpu_seconds",
    "framework_peak_rss_bytes",
    "peak_host_load1",
)


def flatten(summary: dict[str, Any]) -> dict[str, Any]:
    """One flat row from a ``resources.json`` summary (``total_*`` and ``<phase>_*``)."""
    row: dict[str, Any] = {
        "wall_seconds": summary.get("wall_seconds"),
        "link_count": summary.get("link_count"),
        "samples": summary.get("samples"),
        "sampling_errors": summary.get("sampling_errors"),
        "sampler_cpu_seconds": summary.get("sampler_cpu_seconds"),
    }
    total = summary.get("total") or {}
    for metric in TOTAL_METRICS:
        row[f"total_{metric}"] = total.get(metric)
    phases = summary.get("phases") or {}
    for phase in REPORTED_PHASES:
        values = phases.get(phase) or {}
        for metric in PHASE_METRICS:
            row[f"{phase}_{metric}"] = values.get(metric)
    return row


def metric_columns() -> list[str]:
    return list(flatten({}).keys())


def write_csv(path: Path, rows: Iterable[dict[str, Any]], columns: list[str]) -> None:
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def median_summary(
    rows: list[dict[str, Any]], keys: tuple[str, ...], metrics: list[str]
) -> list[dict[str, Any]]:
    """Group ``rows`` by ``keys``; median/min/max of each metric over ``ok`` rows."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(tuple(row.get(key) for key in keys), []).append(row)
    summary = []
    for group, members in groups.items():
        ok = [row for row in members if row.get("status") == "ok"]
        out = dict(zip(keys, group), runs=len(members), ok=len(ok))
        for metric in metrics:
            values = [row[metric] for row in ok if row.get(metric) is not None]
            if values:
                out[f"{metric}_median"] = statistics.median(values)
                out[f"{metric}_min"] = min(values)
                out[f"{metric}_max"] = max(values)
        summary.append(out)
    return summary


def summary_columns(keys: tuple[str, ...], metrics: list[str]) -> list[str]:
    return [*keys, "runs", "ok"] + [
        f"{metric}_{stat}" for metric in metrics for stat in ("median", "min", "max")
    ]
