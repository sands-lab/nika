"""Active benchmark-run progress under ``runtime/benchmark_runs/``."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nika.config import BENCHMARK_RUNS_DIR


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run_display_name(
    model: str | None, agent_type: str | None, created_at: str | None
) -> str:
    """Human-readable run name, such as ``gpt-5 · cli.codex · 2026-10-26 15:00 UTC``."""
    parts = [str(part) for part in (model, agent_type) if part]
    try:
        when = datetime.fromisoformat(str(created_at)).astimezone(timezone.utc)
    except (TypeError, ValueError):
        when = None
    if when is not None:
        parts.append(f"{when:%Y-%m-%d %H:%M} UTC")
    return " · ".join(parts) or "benchmark run"


def progress_path(run_id: str, *, runs_dir: Path | None = None) -> Path:
    root = Path(runs_dir) if runs_dir is not None else BENCHMARK_RUNS_DIR
    return root / f"{run_id}.json"


def write_progress(
    run_id: str,
    *,
    result_dir: str | Path,
    status: str = "running",
    total_trials: int = 0,
    completed_trials: int = 0,
    pending_trials: int = 0,
    benchmark_id: str | None = None,
    version: str | None = None,
    agent_type: str | None = None,
    model: str | None = None,
    run_name: str | None = None,
    runs_dir: Path | None = None,
) -> Path:
    """Create or overwrite the progress document for ``run_id``.

    ``pid`` is the orchestrator process, so readers can tell a live run from
    one that stopped without marking itself finished.
    """
    path = progress_path(run_id, runs_dir=runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = _utc_now_iso()
    payload = {
        "run_id": run_id,
        "result_dir": str(Path(result_dir).resolve()),
        "status": status,
        "total_trials": int(total_trials),
        "completed_trials": int(completed_trials),
        "pending_trials": int(pending_trials),
        "started_at": now,
        "updated_at": now,
        "pid": os.getpid(),
        "benchmark_id": benchmark_id,
        "version": version,
        "agent_type": agent_type,
        "model": model,
        "run_name": run_name or run_display_name(model, agent_type, now),
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def update_progress(
    run_id: str,
    *,
    total_trials: int,
    completed_trials: int,
    pending_trials: int,
    status: str = "running",
    result_dir: str | Path | None = None,
    release_meta: dict[str, Any] | None = None,
    runs_dir: Path | None = None,
) -> Path:
    """Refresh trial counts (and optional identity fields) on an existing doc."""
    path = progress_path(run_id, runs_dir=runs_dir)
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                existing = data
        except (json.JSONDecodeError, OSError):
            existing = {}

    meta = release_meta or {}
    payload = {
        **existing,
        "run_id": run_id,
        "status": status,
        "total_trials": int(total_trials),
        "completed_trials": int(completed_trials),
        "pending_trials": int(pending_trials),
        "updated_at": _utc_now_iso(),
    }
    if result_dir is not None:
        payload["result_dir"] = str(Path(result_dir).resolve())
    for key in ("benchmark_id", "version", "agent_type", "model"):
        if key in meta and meta[key] is not None:
            payload[key] = meta[key]
        elif key not in payload:
            payload[key] = None

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def mark_finished(
    run_id: str,
    *,
    runs_dir: Path | None = None,
) -> Path | None:
    """Set ``status=finished`` on an existing progress document."""
    path = progress_path(run_id, runs_dir=runs_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    data["status"] = "finished"
    data["updated_at"] = _utc_now_iso()
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def list_progress_docs(*, runs_dir: Path | None = None) -> list[dict[str, Any]]:
    """Return every readable progress document."""
    root = Path(runs_dir) if runs_dir is not None else BENCHMARK_RUNS_DIR
    docs: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and data.get("result_dir"):
            data.setdefault("run_id", path.stem)
            docs.append(data)
    return docs


def is_run_live(doc: dict[str, Any]) -> bool:
    """True while a ``running`` document's orchestrator process still exists."""
    if doc.get("status") != "running":
        return False
    try:
        os.kill(int(doc["pid"]), 0)
    except (KeyError, TypeError, ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def annotate_progress(
    run_id: str,
    values: dict[str, Any],
    *,
    runs_dir: Path | None = None,
) -> None:
    """Add ``values`` to an existing progress document; no-op when it is absent."""
    path = progress_path(run_id, runs_dir=runs_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(data, dict):
        return
    data.update(values)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
