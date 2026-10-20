"""Benchmark contract: nothing NIKA copies into an agent sandbox names NIKA.

The host copies the ``agent`` package, the e2b tunnel relay, and the frozen
requirement sets into every agent workspace. Agents must not be able to tell
which benchmark runs them, so none of these files may contain the word, and
the agent package must not import host code.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import agent
from nika.agents.sandbox import tunnel_relay
from nika.agents.sandbox.wheels import REQUIREMENTS_DIR

pytestmark = pytest.mark.contract

_HOST_NAME = re.compile(rb"nika", re.IGNORECASE)


def _sandbox_files() -> list[Path]:
    package = Path(agent.__file__).resolve().parent
    files = [
        path
        for path in package.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ]
    files.append(Path(tunnel_relay.__file__).resolve())
    files.extend(REQUIREMENTS_DIR.glob("*.txt"))
    return files


def test_sandbox_files_do_not_name_the_host() -> None:
    root = Path(agent.__file__).resolve().parents[1]
    leaks = [
        str(path.relative_to(root.parent))
        for path in _sandbox_files()
        if _HOST_NAME.search(path.name.encode()) or _HOST_NAME.search(path.read_bytes())
    ]
    assert leaks == []
