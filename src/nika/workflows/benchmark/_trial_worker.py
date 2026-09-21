"""Spawn-worker entry for a single benchmark trial.

Module-level quiet runs before ``run`` (and MCP FastMCP) is imported, so
library warnings never hit the parent TTY / Rich Live panel.
"""

from __future__ import annotations

from typing import Any

from nika.cli.warning_capture import (
    ignore_known_library_warnings,
    quiet_noisy_loggers,
)

quiet_noisy_loggers()
ignore_known_library_warnings()


def run_trial_worker(**kwargs: Any) -> None:
    from nika.workflows.benchmark.run import _run_trial

    _run_trial(**kwargs)
