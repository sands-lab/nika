"""Codex CLI agents.

Native two-phase orchestration (diagnosis → submission) with workers that
invoke ``codex exec`` subprocesses.

Layout::

    cli/codex/
      agent.py                    # CodexCliAgent — sequential phase runner
      codex_worker.py             # CodexWorker — subprocess adapter
      phases/
        diagnosis.py              # CodexCliDiagnosisPhase
        submission.py             # CodexCliSubmissionPhase
"""

from __future__ import annotations

from typing import Any

__all__ = ["CodexCliAgent"]


def __getattr__(name: str) -> Any:
    if name == "CodexCliAgent":
        from agent.cli.codex.agent import CodexCliAgent

        return CodexCliAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
