"""Read-only scan of ``runtime/benchmark_runs/*.json`` for the inspect UI."""

from __future__ import annotations

import json
from pathlib import Path

from nika.config import BENCHMARK_RUNS_DIR
from nika.inspect.models import BenchmarkProgressDoc


def _parse_progress_file(path: Path) -> BenchmarkProgressDoc | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    run_id = raw.get("run_id") or path.stem
    result_dir = raw.get("result_dir")
    if not run_id or not result_dir:
        return None
    try:
        return BenchmarkProgressDoc(
            run_id=str(run_id),
            result_dir=str(Path(str(result_dir)).resolve()),
            status=str(raw.get("status") or "unknown"),
            total_trials=int(raw.get("total_trials") or 0),
            completed_trials=int(raw.get("completed_trials") or 0),
            pending_trials=int(raw.get("pending_trials") or 0),
            updated_at=str(raw["updated_at"]) if raw.get("updated_at") else None,
            benchmark_id=raw.get("benchmark_id"),
            version=raw.get("version"),
            agent_type=raw.get("agent_type"),
            model=raw.get("model"),
        )
    except (TypeError, ValueError):
        return None


def list_benchmark_progress(
    *,
    runs_dir: Path | None = None,
    status: str | None = "running",
    under: Path | str | None = None,
) -> list[BenchmarkProgressDoc]:
    """List progress docs, optionally filtered by status and path containment.

    ``under`` keeps docs whose ``result_dir`` equals or contains ``under``,
    or whose ``result_dir`` is an ancestor of ``under`` (viewer inside the job).
    """
    root = Path(runs_dir) if runs_dir is not None else BENCHMARK_RUNS_DIR
    if not root.is_dir():
        return []

    under_abs: Path | None = None
    if under is not None:
        under_abs = Path(under).resolve()

    items: list[BenchmarkProgressDoc] = []
    for path in sorted(root.glob("*.json")):
        doc = _parse_progress_file(path)
        if doc is None:
            continue
        if status is not None and doc.status != status:
            continue
        if under_abs is not None and not _paths_overlap(doc.result_dir, under_abs):
            continue
        items.append(doc)

    items.sort(key=lambda d: (d.updated_at or "", d.run_id), reverse=True)
    return items


def _paths_overlap(result_dir: str, under: Path) -> bool:
    """True if ``under`` is inside ``result_dir`` or ``result_dir`` is inside ``under``."""
    try:
        job = Path(result_dir).resolve()
    except OSError:
        return False
    if under == job:
        return True
    try:
        under.relative_to(job)
        return True
    except ValueError:
        pass
    try:
        job.relative_to(under)
        return True
    except ValueError:
        return False
