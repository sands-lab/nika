"""Calculate per-path latency baselines from healthy recorded probes."""

from __future__ import annotations

import math
import time
from itertools import permutations
from pathlib import Path
from statistics import median

from nika.service.kathara.prometheus_api import prometheus_query_for
from nika.utils.session_artifacts import write_json_atomic
from nika.utils.logger import system_logger

BASELINE_FILE = "pingmesh-latency-baseline.json"
# Ten minutes accommodates an isolated Clos calibration while bounding large meshes.
CALIBRATION_TIMEOUT_SECONDS = 600
RTT_QUERY = 'probe_icmp_duration_seconds{phase="rtt",expected="reachable"}'


def healthy_latency_baselines(
    rows: list[dict], *, after: float
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


def capture_latency_baseline(
    api, directory: str, intent
) -> dict[tuple[str, str], float | None]:
    """Record post-readiness RTTs for every supported reachable directed path."""
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
    started = time.time()
    start_monotonic = time.monotonic()
    deadline = start_monotonic + CALIBRATION_TIMEOUT_SECONDS
    baselines = {}
    while expected - baselines.keys():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(15, remaining))
        elapsed = max(1, math.ceil(time.time() - started))
        rows = prometheus_query_for(api, query=f"{RTT_QUERY}[{elapsed}s]", limit=None)
        baselines = healthy_latency_baselines(rows, after=started)
    missing = expected - baselines.keys()
    if missing:
        system_logger.warning(
            "Pingmesh latency baseline unavailable for %d of %d paths after %.1f seconds; reachability checks remain active.",
            len(missing),
            len(expected),
            time.monotonic() - start_monotonic,
        )
    system_logger.info(
        "Pingmesh latency calibration completed in %.1f seconds (%d/%d paths).",
        time.monotonic() - start_monotonic,
        len(expected) - len(missing),
        len(expected),
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


def read_latency_baseline(directory: str) -> dict[tuple[str, str], float | None]:
    """Read the healthy path RTTs stored with the session."""
    import json

    rows = json.loads((Path(directory) / BASELINE_FILE).read_text())
    return {(row["source"], row["target"]): row["rtt_seconds"] for row in rows}
