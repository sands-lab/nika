"""Unit tests for session-bound system logger."""

from __future__ import annotations

import json
from pathlib import Path

from nika.utils.logger import bind_session_dir, log_event


def test_bind_session_dir_accepts_str_and_path(tmp_path: Path) -> None:
    bind_session_dir(str(tmp_path / "a"))
    assert (tmp_path / "a" / "events.jsonl").exists() or (tmp_path / "a").is_dir()
    bind_session_dir(tmp_path / "b")
    assert (tmp_path / "b").is_dir()


def test_log_event_writes_top_level_duration_ms(tmp_path: Path) -> None:
    bind_session_dir(tmp_path)
    log_event("env_verify", "ok", scenario="simple_bgp", duration_ms=12.5)
    rows = [
        json.loads(line)
        for line in (tmp_path / "nika.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["event"] == "env_verify"
    assert rows[0]["duration_ms"] == 12.5
    assert "duration_ms" not in (rows[0].get("data") or {})
    assert rows[0]["data"]["scenario"] == "simple_bgp"
    assert rows[0]["timestamp"].endswith("+00:00") or rows[0]["timestamp"].endswith("Z")
