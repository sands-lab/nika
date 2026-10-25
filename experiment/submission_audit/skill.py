"""Import path to the nika-submission-audit skill's library (``auditlib``)."""

from __future__ import annotations

import sys

from nika.config import REPO_ROOT

SKILL_DIR = REPO_ROOT / ".agents" / "skills" / "nika-submission-audit"
SCRIPTS = SKILL_DIR / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
