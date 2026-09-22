"""Unit tests for session result-directory helpers."""

from __future__ import annotations

import json
from pathlib import Path

from nika.utils.session_artifacts import last_session_error


def _write_events(session_dir: Path, records: list[dict | str]) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    (session_dir / "nika.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_last_session_error_returns_most_recent_error(tmp_path: Path) -> None:
    _write_events(
        tmp_path,
        [
            {"level": "ERROR", "event": "env_start_failed", "message": "first boom"},
            {"level": "INFO", "event": "env_stop", "message": "stopped"},
            {"level": "ERROR", "event": "inject_failed", "message": "second boom"},
            {"level": "INFO", "event": "session_cleared", "message": "cleared"},
        ],
    )
    assert last_session_error(tmp_path) == "second boom"


def test_last_session_error_skips_malformed_and_empty_lines(tmp_path: Path) -> None:
    _write_events(
        tmp_path,
        [
            {"level": "ERROR", "event": "env_start_failed", "message": "real cause"},
            "",
            "{not json",
            {"level": "ERROR", "event": "noop", "message": "   "},
        ],
    )
    assert last_session_error(tmp_path) == "real cause"


def test_last_session_error_without_errors_or_log(tmp_path: Path) -> None:
    assert last_session_error(tmp_path / "missing") is None
    _write_events(tmp_path, [{"level": "INFO", "event": "env_start", "message": "ok"}])
    assert last_session_error(tmp_path) is None
