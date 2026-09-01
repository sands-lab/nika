"""Shared helpers for scenario-level failure compatibility sweeps."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def probe_report_enabled() -> bool:
    return os.environ.get("NIKA_TEST_PROBE_REPORT") == "1"


def write_probe_report(path: Path, payload: dict[str, Any]) -> None:
    if not probe_report_enabled():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
