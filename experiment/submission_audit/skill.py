"""Import path to the nika-submission-audit skill's library (``auditlib``),
and the helpers every experiment script shares."""

from __future__ import annotations

import random
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / ".agents" / "skills" / "nika-submission-audit"
SCRIPTS = SKILL_DIR / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from auditlib.kb import KB_DIR  # noqa: E402
from auditlib.trace import trial_dirs  # noqa: E402


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
