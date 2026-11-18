"""DeepSeek Harness CLI agents.

Native two-phase orchestration (diagnosis → submission) with workers that
invoke ``dsh --profile headless`` subprocesses.

Layout::

    cli/dsh/
      agent.py                    # DshAgent — sequential phase runner
      config.py                   # Isolated home, PATH, and credential aliases
      dsh_worker.py               # DshWorker — subprocess adapter
      phases/
        diagnosis.py              # DshDiagnosisPhase
        submission.py             # DshSubmissionPhase
"""

from __future__ import annotations

from typing import Any

__all__ = ["DshAgent"]


def __getattr__(name: str) -> Any:
    if name == "DshAgent":
        from agent.cli.dsh.agent import DshAgent

        return DshAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
