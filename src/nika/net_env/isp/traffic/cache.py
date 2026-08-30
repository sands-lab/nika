"""Normalized dynamic traffic cache under ``.nika_cache/sndlib/traffic/``."""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

from nika.config import REPO_ROOT
from nika.net_env.isp.traffic.models import (
    DEFAULT_DEMAND_INTERVAL_SEC,
    TrafficFlow,
    TrafficInterval,
    TrafficMatrixSeries,
)

# Known official / community packages that convert into NIKA cache layout.
# URLs may be overridden later; fetch fails clearly if download is unavailable.
DYNAMIC_TRAFFIC_CATALOG: dict[str, dict[str, Any]] = {
    "abilene": {
        "duration_sec": 300,
        "description": "Abilene 5-minute OD matrices (normalized NIKA cache).",
        # Placeholder: users may place pre-normalized cache; fetch tries this URL.
        "source_url": None,
    },
    "geant": {
        "duration_sec": 900,
        "description": "GÉANT 15-minute OD matrices (normalized NIKA cache).",
        "source_url": None,
    },
}


def default_cache_root() -> Path:
    return REPO_ROOT / ".nika_cache"


def dynamic_cache_dir(topology: str, *, cache_root: Path | None = None) -> Path:
    root = cache_root if cache_root is not None else default_cache_root()
    return root / "sndlib" / "traffic" / topology


def load_dynamic_series(
    cache_dir: Path,
    *,
    topology: str | None = None,
) -> TrafficMatrixSeries:
    """Load a normalized dynamic series from ``manifest.json`` + ``intervals/``."""
    manifest_path = cache_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Missing traffic manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    topo_name = str(manifest.get("topology") or topology or cache_dir.name)
    duration = int(manifest.get("duration_sec") or DEFAULT_DEMAND_INTERVAL_SEC)
    sample = manifest.get("sample_period_sec")
    sample_period = int(sample) if sample is not None else duration
    count = int(manifest.get("interval_count") or 0)
    intervals_dir = cache_dir / "intervals"
    if not intervals_dir.is_dir():
        raise FileNotFoundError(f"Missing intervals directory: {intervals_dir}")

    intervals: list[TrafficInterval] = []
    if count <= 0:
        # Discover sorted interval files.
        files = sorted(intervals_dir.glob("*.json"))
        count = len(files)
    for index in range(count):
        path = intervals_dir / f"{index:06d}.json"
        if not path.is_file():
            raise FileNotFoundError(f"Missing interval file: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        intervals.append(
            _interval_from_payload(payload, index=index, duration_sec=duration)
        )

    if not intervals:
        raise ValueError(f"Dynamic traffic cache for {topo_name!r} has no intervals.")

    return TrafficMatrixSeries(
        topology=topo_name,
        source="dynamic",
        intervals=tuple(intervals),
        sample_period_sec=sample_period,
        unit_note=str(manifest.get("unit_note") or "SNDlib / trace units (not Mbps)"),
        path=str(cache_dir.resolve()),
    )


def write_normalized_series(
    series: TrafficMatrixSeries,
    cache_dir: Path,
    *,
    source_url: str | None = None,
) -> Path:
    """Write a series into the normalized cache layout (idempotent overwrite)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    intervals_dir = cache_dir / "intervals"
    intervals_dir.mkdir(parents=True, exist_ok=True)
    # Clear stale interval files.
    for old in intervals_dir.glob("*.json"):
        old.unlink()

    for interval in series.intervals:
        path = intervals_dir / f"{interval.index:06d}.json"
        payload = {
            "index": interval.index,
            "duration_sec": interval.duration_sec,
            "flows": [
                {
                    "src": f.src_node_id,
                    "dst": f.dst_node_id,
                    "rate": f.rate,
                }
                for f in interval.flows
            ],
        }
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    manifest = {
        "topology": series.topology,
        "source": "dynamic",
        "interval_count": len(series.intervals),
        "duration_sec": series.intervals[0].duration_sec if series.intervals else 0,
        "sample_period_sec": series.sample_period_sec,
        "unit_note": series.unit_note,
        "source_url": source_url,
    }
    manifest_path = cache_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return cache_dir


def fetch_dynamic_traffic(
    topology: str,
    *,
    cache_root: Path | None = None,
    force: bool = False,
) -> Path:
    """Download and normalize dynamic traffic for a known topology.

    If the catalog has no downloadable URL, raises with guidance to use demands
    or place a normalized cache manually. When a URL is present, downloads and
    converts via the topology adapter.
    """
    topo = topology.strip().lower()
    entry = DYNAMIC_TRAFFIC_CATALOG.get(topo)
    if entry is None:
        known = ", ".join(sorted(DYNAMIC_TRAFFIC_CATALOG))
        raise ValueError(
            f"No dynamic traffic fetch adapter for topology {topology!r}. "
            f"Known: {known}. Use `nika traffic run sndlib --mode demands` or "
            f"write a normalized cache under .nika_cache/sndlib/traffic/{topo}/."
        )

    cache_dir = dynamic_cache_dir(topo, cache_root=cache_root)
    manifest = cache_dir / "manifest.json"
    if manifest.is_file() and not force:
        return cache_dir

    source_url = entry.get("source_url")
    if not source_url:
        raise ValueError(
            f"Official download URL for {topo!r} is not configured in NIKA yet. "
            f"Place a normalized cache at {cache_dir} "
            f"(manifest.json + intervals/NNNNNN.json), or use "
            f"`nika traffic run sndlib --mode demands`."
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    raw_path = cache_dir / "raw_download.bin"
    urllib.request.urlretrieve(str(source_url), raw_path)  # noqa: S310
    series = _convert_raw_download(
        topo, raw_path, duration_sec=int(entry["duration_sec"])
    )
    write_normalized_series(series, cache_dir, source_url=str(source_url))
    return cache_dir


def _interval_from_payload(
    payload: dict[str, Any], *, index: int, duration_sec: int
) -> TrafficInterval:
    dur = int(payload.get("duration_sec") or duration_sec)
    flows_raw = payload.get("flows")
    flows: list[TrafficFlow] = []
    if flows_raw is not None:
        for item in flows_raw:
            src = str(item["src"])
            dst = str(item["dst"])
            rate = float(item["rate"])
            if src == dst or rate <= 0:
                continue
            flows.append(TrafficFlow(src_node_id=src, dst_node_id=dst, rate=rate))
    else:
        # Dense matrix + node_order
        node_order = [str(n) for n in payload.get("node_order") or []]
        matrix = payload.get("matrix")
        if not node_order or matrix is None:
            raise ValueError(f"Interval {index} missing flows or matrix/node_order")
        for i, src in enumerate(node_order):
            row = matrix[i]
            for j, dst in enumerate(node_order):
                rate = float(row[j])
                if i == j or rate <= 0:
                    continue
                flows.append(TrafficFlow(src_node_id=src, dst_node_id=dst, rate=rate))
    flows_t = tuple(sorted(flows, key=lambda f: (f.src_node_id, f.dst_node_id, f.rate)))
    return TrafficInterval(index=index, duration_sec=dur, flows=flows_t)


def _convert_raw_download(
    topology: str, raw_path: Path, *, duration_sec: int
) -> TrafficMatrixSeries:
    """Convert a downloaded raw package into TrafficMatrixSeries.

    Adapters are topology-specific; extend as official URLs are wired.
    """
    raise ValueError(
        f"Raw converter for {topology!r} is not implemented for {raw_path.name}. "
        "Provide normalized cache JSON instead."
    )
