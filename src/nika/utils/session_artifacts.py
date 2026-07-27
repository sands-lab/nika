"""Session result directory helpers (no evaluator/agent imports)."""

from __future__ import annotations

from pathlib import Path

from nika.config import RESULTS_DIR

RUN_FILENAME = "run.json"


def is_finished_session(run_meta: dict) -> bool:
    if run_meta.get("status") == "finished":
        return True
    return run_meta.get("end_time") is not None


def iter_session_dirs(results_dir: str | Path | None = None) -> list[Path]:
    """Discover session/trial dirs that contain ``run.json``.

    Supports flat ``{root}/{session_id}/`` and one nesting level such as
    trial runs under ``{root}/trials/{trial_id}/``. Skips ``0_summary``.
    Root-level ``run.json`` (run config) is not a session directory.
    """
    root = Path(results_dir or RESULTS_DIR)
    if not root.exists():
        return []
    session_dirs: list[Path] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name == "0_summary":
            continue
        if (entry / RUN_FILENAME).exists():
            session_dirs.append(entry)
            continue
        for sub in sorted(entry.iterdir()):
            if sub.is_dir() and (sub / RUN_FILENAME).exists():
                session_dirs.append(sub)
    return session_dirs
