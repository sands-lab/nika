"""Session result directory helpers (no evaluator/agent imports)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from nika.config import RESULTS_DIR

RUN_FILENAME = "run.json"


def write_text_atomic(path: str | Path, text: str) -> None:
    """Replace *path* with *text* so concurrent readers never see a partial file.

    A benchmark engine and its frontend read and write the same session files.
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
