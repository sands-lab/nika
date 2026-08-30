"""Unit tests for session-bound system logger."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nika.utils.logger import bind_session_dir


def test_bind_session_dir_accepts_str_and_path(tmp_path: Path) -> None:
    bind_session_dir(str(tmp_path / "a"))
    assert (tmp_path / "a" / "events.jsonl").exists() or (tmp_path / "a").is_dir()
    bind_session_dir(tmp_path / "b")
    assert (tmp_path / "b").is_dir()


def test_bind_session_dir_rejects_magicmock() -> None:
    session = MagicMock(name="Session()")
    with pytest.raises(TypeError, match="str or Path"):
        bind_session_dir(session.session_dir)
