"""Session result directory helpers (no evaluator/agent imports)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from nika.config import RESULTS_DIR

RUN_FILENAME = "run.json"

SessionStatus = Literal["running", "finished", "aborted", "error"]

_TERMINAL_STATUSES = frozenset({"finished", "aborted", "error", "interrupted", "failed"})


def is_finished_session(run_meta: dict) -> bool:
    """True when the session is no longer actively running.

    Includes normal finish as well as aborted/error terminals (any end_time
    or explicit non-running status). Callers that need only successful
    completion should check ``normalize_session_status(...) == "finished"``.
    """
    status = normalize_session_status(run_meta)
    if status != "running":
        return True
    return run_meta.get("end_time") is not None


def normalize_session_status(run_meta: dict) -> SessionStatus:
    """Map ``run.json`` status / end_time into the inspect status chip set."""
    raw = str(run_meta.get("status") or "").strip().lower()
    if raw in {"aborted", "interrupted"}:
        return "aborted"
    if raw in {"error", "failed"}:
        return "error"
    if raw == "finished":
        return "finished"
    if raw == "running" or not raw:
        if run_meta.get("end_time") is not None:
            return "finished"
        return "running"
    # Unknown explicit status with an end_time → treat as finished terminal.
    if run_meta.get("end_time") is not None or raw in _TERMINAL_STATUSES:
        return "finished"
    return "running"


def iter_session_dirs(results_dir: str | Path | None = None) -> list[Path]:
    """Discover session/trial dirs that contain ``run.json``.

    Walks the results tree recursively and keeps only **session leaves**:
    directories with ``run.json`` that are not containers for nested trials
    (a dir with both ``run.json`` and a ``trials/`` child that itself holds
    sessions is treated as a job/run folder, not a session).

    Skips ``0_summary`` and hidden directories. Caps depth to avoid runaway walks.
    """
    root = Path(results_dir or RESULTS_DIR)
    if not root.is_dir():
        return []

    skip_names = {"0_summary", "node_modules", "__pycache__"}
    session_dirs: list[Path] = []

    def is_session_leaf(path: Path) -> bool:
        if not (path / RUN_FILENAME).is_file():
            return False
        trials = path / "trials"
        if not trials.is_dir():
            return True
        try:
            for child in trials.iterdir():
                if child.is_dir() and (child / RUN_FILENAME).is_file():
                    return False
        except OSError:
            return True
        return True

    def walk(dir_path: Path, depth: int) -> None:
        if depth > 16:
            return
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir():
                continue
            name = entry.name
            if name.startswith(".") or name in skip_names:
                continue
            if is_session_leaf(entry):
                session_dirs.append(entry)
                continue
            walk(entry, depth + 1)

    walk(root, 0)
    return session_dirs
