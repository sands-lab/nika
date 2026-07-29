"""Claude Code CLI agents.

Native two-phase orchestration (diagnosis → submission) with workers that
invoke ``claude -p`` subprocesses.  Model defaults and authentication are
handled by :mod:`agent.cli.claude.config`.

Layout::

    cli/claude/
      agent.py                    # ClaudeAgent — sequential phase runner
      config.py                   # Model defaults and auth helpers
      claude_worker.py            # ClaudeWorker — subprocess adapter
      claude_display.py           # Claude stream-json event formatter
      phases/
        diagnosis.py              # ClaudeDiagnosisPhase
        submission.py             # ClaudeSubmissionPhase
"""

from __future__ import annotations

from typing import Any

__all__ = ["ClaudeAgent"]


def __getattr__(name: str) -> Any:
    if name == "ClaudeAgent":
        from agent.cli.claude.agent import ClaudeAgent

        return ClaudeAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
