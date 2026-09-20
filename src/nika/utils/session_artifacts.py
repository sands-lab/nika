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
