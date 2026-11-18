#!/usr/bin/env python
"""Run local output parsers from the SADE working directory."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable

SKILLS_DIR = ROOT / ".claude" / "skills"

# Local parsers for saved MCP output.
SCRIPTS = {
    "parse_large": SKILLS_DIR
    / "big-return-skill"
    / "scripts"
    / "parse_large_output.py",
}


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("usage: python h.py <script_name> [args...]\n")
        for name in SCRIPTS:
            sys.stderr.write(f"  {name}\n")
        return 2

    name = sys.argv[1]

    if name in SCRIPTS:
        return subprocess.call([PYTHON, str(SCRIPTS[name])] + sys.argv[2:])

    sys.stderr.write(f"helper not found: {name}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
