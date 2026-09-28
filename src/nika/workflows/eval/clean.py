"""Remove persisted evaluation artifacts and runtime session documents."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from nika.config import resolve_results_root, SESSIONS_DIR
from nika.utils.session_index import SessionIndex
from nika.utils.session_store import SessionStore


@dataclass(frozen=True)
class EvalCleanReport:
    session_files_removed: int
    results_entries_removed: int


def run_eval_clean(
    *,
    results_dir: str | Path | None = None,
    sessions_dir: str | Path | None = None,
    db_path: str | Path | None = None,
    force: bool = False,
) -> EvalCleanReport:
    """Delete all contents under the results root, session JSON files, and the index."""
    results_root = Path(results_dir or resolve_results_root())
    sessions_root = Path(sessions_dir or SESSIONS_DIR)
    index = SessionIndex(db_path)

    running = SessionStore(
        sessions_root, db_path or index.db_path
    ).list_running_sessions()
    if running and not force:
        ids = ", ".join(str(row.get("session_id", "?")) for row in running)
        raise ValueError(
            f"{len(running)} running session(s) found ({ids}). "
            "Close them with `nika session close` first, or pass --force."
        )

    session_files_removed = 0
    if sessions_root.exists():
        for path in sessions_root.glob("*.json"):
            path.unlink()
            session_files_removed += 1

    results_entries_removed = 0
    if results_root.exists():
        for path in results_root.iterdir():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            results_entries_removed += 1

    index.truncate()

    return EvalCleanReport(
        session_files_removed=session_files_removed,
        results_entries_removed=results_entries_removed,
    )
