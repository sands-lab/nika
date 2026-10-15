"""Per-session resource summary written next to the session's results."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from nika.metrics.stack import PROMETHEUS_URL
from nika.utils.session_artifacts import write_json_atomic

RESOURCES_FILENAME = "resources.json"

# Prometheus scrapes and evaluates rules every 5 s; wait at most this long for
# a sample taken after teardown started, so the summary covers the whole run.
_FRESH_SAMPLE_TIMEOUT_SEC = 12.0


def _query(client: httpx.Client, expr: str) -> dict[str, float]:
    """Instant query; map each result's ``node`` label (or "") to its value."""
    resp = client.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": expr})
    resp.raise_for_status()
    return {
        row["metric"].get("node", ""): float(row["value"][1])
        for row in resp.json()["data"]["result"]
    }


def _rounded(result: dict[str, float], digits: int | None = None) -> float | None:
    """Single-value query result, or ``None`` when Prometheus has no sample."""
    if "" not in result:
        return None
    return round(result[""], digits) if digits else int(result[""])


def _wait_for_fresh_sample(client: httpx.Client, selector: str, after: float) -> None:
    deadline = time.monotonic() + _FRESH_SAMPLE_TIMEOUT_SEC
    while time.monotonic() < deadline:
        latest = _query(
            client, f"max(timestamp(nika:session_memory_working_set_bytes{selector}))"
        )
        if not latest or latest[""] >= after:
            return
        time.sleep(1.0)


def write_session_resources(session_meta: dict[str, Any]) -> Path | None:
    """Query Prometheus for the session's usage so far and write ``resources.json``.

    Returns ``None`` when the session has no recorded samples.
    """
    session_id = session_meta["session_id"]
    started = datetime.fromisoformat(session_meta["created_at"])
    ended = datetime.now(timezone.utc)
    duration = (ended - started).total_seconds()
    window = f"{math.ceil(duration) + 60}s"
    sel = f'{{session_id="{session_id}"}}'

    with httpx.Client(timeout=10.0) as client:
        _wait_for_fresh_sample(client, sel, ended.timestamp())
        node_cpu = _query(
            client,
            f"sum by (node) (max_over_time(nika:container_cpu_seconds_total{sel}[{window}]))",
        )
        node_mem = _query(
            client,
            f"max by (node) (max_over_time(nika:container_memory_working_set_bytes{sel}[{window}]))",
        )
        if not node_cpu and not node_mem:
            return None
        session_cpu_peak = _query(
            client,
            f"max(max_over_time(nika:session_cpu_cores{sel}[{window}]))",
        )
        session_mem_peak = _query(
            client,
            f"max(max_over_time(nika:session_memory_working_set_bytes{sel}[{window}]))",
        )

    cpu_seconds = sum(node_cpu.values())
    summary = {
        "session_id": session_id,
        "scenario": session_meta.get("scenario_name"),
        "backend": session_meta.get("backend"),
        "lab_name": session_meta.get("lab_name"),
        "start": started.isoformat(),
        "end": ended.isoformat(),
        "duration_sec": round(duration, 1),
        "cpu_seconds": round(cpu_seconds, 2),
        "cpu_cores_avg": round(cpu_seconds / duration, 3) if duration > 0 else None,
        "cpu_cores_peak": _rounded(session_cpu_peak, 3),
        "memory_peak_bytes": _rounded(session_mem_peak),
        "nodes": {
            node: {
                "cpu_seconds": round(node_cpu.get(node, 0.0), 2),
                "memory_peak_bytes": int(node_mem.get(node, 0.0)),
            }
            for node in sorted(set(node_cpu) | set(node_mem))
        },
        "source": PROMETHEUS_URL,
    }
    path = Path(session_meta["session_dir"]) / RESOURCES_FILENAME
    write_json_atomic(path, summary)
    return path
