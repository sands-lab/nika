"""Shared helpers for the submission-audit experiment."""

from __future__ import annotations

import random
import shutil
from pathlib import Path

from nika.audit.kb import KB_DIR
from nika.audit.trace import trial_dirs


def builtin_kb_dir(dest: Path) -> Path:
    """Copy the skill's built-in part (no learned issues) to *dest*."""
    shutil.copytree(KB_DIR, dest, ignore=shutil.ignore_patterns("issues"))
    (dest / "issues").mkdir()
    return dest


def submissions(root: Path, order: str, shuffle_seed: int) -> list[Path]:
    """Leaderboard submission packages under *root*, by date prefix or shuffled."""
    paths = sorted(p for p in Path(root).iterdir() if p.is_dir() and trial_dirs(p))
    if order == "shuffle":
        random.Random(shuffle_seed).shuffle(paths)
    return paths
