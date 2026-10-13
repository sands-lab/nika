"""Spawn-worker entry for a single benchmark trial.

Module-level quiet runs before ``run`` (and MCP FastMCP) is imported, so
library warnings never hit the parent TTY / Rich Live panel.
"""

from __future__ import annotations

import signal
from typing import Any

from nika.cli.warning_capture import (
    ignore_known_library_warnings,
    quiet_noisy_loggers,
)

quiet_noisy_loggers()
ignore_known_library_warnings()


def _raise_on_sigterm(signum: int, frame: Any) -> None:
    """Turn the parent's SIGTERM into ``SystemExit`` so ``finally`` blocks run.

    Cleanup (sandbox removal, MCP gateway shutdown, lab undeploy) lives in
    ``finally`` / interrupt handlers that a default SIGTERM would skip. Later
    SIGTERMs are ignored so they cannot abort that cleanup; the parent still
    SIGKILLs after its grace period.
    """
    del frame
    # A Python-level no-op (not SIG_IGN) so exec'd cleanup children keep the
    # default SIGTERM disposition.
    signal.signal(signal.SIGTERM, lambda *_: None)
    raise SystemExit(128 + signum)


def run_trial_worker(**kwargs: Any) -> None:
    signal.signal(signal.SIGTERM, _raise_on_sigterm)
    from nika.workflows.benchmark.run import _run_trial

    _run_trial(**kwargs)
