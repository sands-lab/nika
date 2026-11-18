"""OpenCode CLI agents.

Native two-phase orchestration (diagnosis → submission) with workers that
invoke ``opencode run`` subprocesses.

Layout::

    cli/opencode/
      agent.py                    # OpenCodeAgent — sequential phase runner
      opencode_worker.py          # OpenCodeWorker — subprocess adapter
      phases/
        diagnosis.py              # OpenCodeDiagnosisPhase
        submission.py             # OpenCodeSubmissionPhase
"""

from __future__ import annotations

from typing import Any

__all__ = ["OpenCodeAgent"]


def __getattr__(name: str) -> Any:
    if name == "OpenCodeAgent":
        from agent.cli.opencode.agent import OpenCodeAgent

        return OpenCodeAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
