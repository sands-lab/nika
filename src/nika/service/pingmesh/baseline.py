"""Calculate per-path latency baselines from healthy recorded probes."""

from __future__ import annotations

import math
import fcntl
import json
import time
from itertools import permutations
from pathlib import Path
from statistics import median

from nika.service.kathara.prometheus_api import prometheus_query_for
from nika.utils.session_artifacts import write_json_atomic

BASELINE_FILE = "pingmesh-latency-baseline.json"
WINDOW_FILE = "pingmesh-latency-window.json"
RTT_QUERY = 'probe_icmp_duration_seconds{phase="rtt",expected="reachable"}'


def healthy_latency_baselines(
    rows: list[dict], *, after: float, before: float = math.inf
) -> dict[tuple[str, str], float]:
    """Return the median RTT for paths with at least three post-readiness samples."""
    samples: dict[tuple[str, str], dict[float, float]] = {}
    for row in rows:
        labels = row["metric"]
        path = (labels["source"], labels["target"])
        points = samples.setdefault(path, {})
        for stamp, value in row["values"]:
            if (
                isinstance(stamp, (int, float))
                and stamp >= after
                and stamp <= before
                and isinstance(value, (int, float))
                and math.isfinite(value)
                and value > 0
            ):
                points[stamp] = value
    return {
        path: median(points.values())
        for path, points in samples.items()
        if len(points) >= 3
    }


def sustained_high_latency(samples: list, threshold: float) -> int:
    """Return the high-scrape count when at least two are a strict majority."""
    high = sum(
        isinstance(value, (int, float)) and value > threshold for value in samples
    )
    return high if high >= 2 and high > len(samples) / 2 else 0


def initialize_latency_baseline(directory: str) -> None:
    """Start a healthy sampling window without waiting for probe coverage."""
    write_json_atomic(Path(directory) / BASELINE_FILE, [])
    write_json_atomic(
        Path(directory) / WINDOW_FILE,
        {"started_at": time.time(), "frozen_at": None},
    )


def capture_latency_baseline(
    api, directory: str, intent, *, freeze: bool = False
) -> dict[tuple[str, str], float | None]:
    """Read available healthy samples once, optionally freezing before injection."""
    window_path = Path(directory) / WINDOW_FILE
    # Older snapshots carry a fixed baseline, with no learning window.
    if not window_path.exists():
        return read_latency_baseline(directory)
    with (Path(directory) / "pingmesh-latency.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        window = json.loads(window_path.read_text())
        if window["frozen_at"] is not None:
            return read_latency_baseline(directory)
        before = time.time()
        if freeze:
            window["frozen_at"] = before
            write_json_atomic(window_path, window)
        return _capture_available(api, directory, intent, window["started_at"], before)


def _capture_available(api, directory: str, intent, after: float, before: float):
    saved = read_latency_baseline(directory)
    supported = prometheus_query_for(
        api, query="pingmesh_endpoint_supported == 1", limit=None
    )
    endpoints = {row["node"] for row in supported if row.get("node")}
    if not endpoints:
        raise RuntimeError(
            "Pingmesh reports no supported endpoints for latency calibration."
        )
    isolated = intent.isolation_matcher() if intent else lambda _a, _b: False
    expected = {(a, b) for a, b in permutations(endpoints, 2) if not isolated(a, b)}
    expected.update(saved)
    elapsed = max(1, math.ceil(before - after))
    rows = prometheus_query_for(api, query=f"{RTT_QUERY}[{elapsed}s]", limit=None)
    baselines = healthy_latency_baselines(rows, after=after, before=before)
    baselines.update(
        {path: value for path, value in saved.items() if value is not None}
    )
    write_json_atomic(
        Path(directory) / BASELINE_FILE,
        [
            {
                "source": source,
                "target": target,
                "rtt_seconds": baselines.get((source, target)),
            }
            for source, target in sorted(expected)
        ],
    )
    return {path: baselines.get(path) for path in expected}


def freeze_session_latency_baseline(meta: dict) -> None:
    """Freeze available healthy RTT evidence before the first fault mutation."""
    from nika.net_env.probe_intent import ProbeIntent
    from nika.runtime.factory import resolve_backend
    from nika.service.lab.host_api import create_host_api
    from nika.telemetry import PINGMESH, telemetry_for_session

    if PINGMESH not in telemetry_for_session(meta):
        return
    api = create_host_api(
        lab_name=meta["lab_name"], backend=resolve_backend(meta), session_meta=meta
    )
    capture_latency_baseline(
        api,
        meta["session_dir"],
        ProbeIntent.from_metadata(meta.get("metadata")),
        freeze=True,
    )


def read_latency_baseline(directory: str) -> dict[tuple[str, str], float | None]:
    """Read the healthy path RTTs stored with the session."""
    rows = json.loads((Path(directory) / BASELINE_FILE).read_text())
    return {(row["source"], row["target"]): row["rtt_seconds"] for row in rows}
