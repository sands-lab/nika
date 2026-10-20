#!/usr/bin/env python
"""Tiny launcher for diagnosis-methodology helper scripts.

Usage from the agent's working directory (this package directory):
    python h.py <script_name> [args...]

`script_name` may include or omit the `.py` suffix. The launcher locates the
script under `.claude/skills/diagnosis-methodology-skill/scripts/` (next to
this file) and runs it with the current interpreter. This exists so the agent does not have to remember
(or typo) the long path to each helper.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable

SKILLS_DIR = ROOT / ".claude" / "skills"
SCRIPTS = SKILLS_DIR / "diagnosis-methodology-skill" / "scripts"

# Special-purpose scripts that live outside the diagnosis-methodology folder.
EXTRA_SCRIPTS = {
    "parse_large": SKILLS_DIR
    / "big-return-skill"
    / "scripts"
    / "parse_large_output.py",
    "bgp_snapshot": SKILLS_DIR / "bgp-fault-skill" / "scripts" / "bgp_snapshot.py",
}


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write(
            "usage: python h.py <script_name> [args...]\n"
            f"available scripts under {SCRIPTS}:\n"
        )
        if SCRIPTS.is_dir():
            for name in sorted(p.name for p in SCRIPTS.glob("*.py")):
                if not name.startswith("_"):
                    sys.stderr.write(f"  {name[:-3]}\n")
        sys.stderr.write("special:\n")
        for name in EXTRA_SCRIPTS:
            sys.stderr.write(f"  {name}\n")
        return 2

    name = sys.argv[1]

    if name in EXTRA_SCRIPTS:
        return subprocess.call([PYTHON, str(EXTRA_SCRIPTS[name])] + sys.argv[2:])

    if not name.endswith(".py"):
        name += ".py"
    script_path = SCRIPTS / name
    if not script_path.is_file():
        sys.stderr.write(f"helper not found: {script_path}\n")
        return 2

    return subprocess.call([PYTHON, str(script_path)] + sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
