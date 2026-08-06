"""Claude Code CLI troubleshooting agent.

Two-phase pipeline via ``claude -p`` subprocesses (no LangGraph).

* **diagnosis phase** → :class:`~agent.cli.claude.phases.ClaudeDiagnosisPhase`
  (``claude -p`` with Kathara MCP servers; server set chosen dynamically
  based on the session scenario)
* **submission phase** → :class:`~agent.cli.claude.phases.ClaudeSubmissionPhase`
  (``claude -p`` with the task MCP server; calls ``submit()`` to record
  a structured result)

Authentication uses ``NIKA_LLM_PROVIDER`` with ``ANTHROPIC_API_KEY``,
``DEEPSEEK_API_KEY``, or ``NIKA_CUSTOM_*`` (mapped for the subprocess only),
or ``claude auth login``.  See :mod:`agent.cli.claude.config` and
``src/agent/README.md``.

Select with ``nika agent run -a cli.claude``.
"""

from __future__ import annotations

import sys
from typing import Any

from agent.cli.claude.config import resolve_claude_model
from agent.cli.claude.phases.diagnosis import ClaudeDiagnosisPhase
from agent.cli.claude.phases.submission import ClaudeSubmissionPhase
from agent.sandbox.session_dir import resolve_agent_session_dir
from agent.utils.phases import DIAGNOSIS, SUBMISSION
from nika.utils.session import Session


class ClaudeAgent:
    """Two-phase troubleshooting agent backed by Claude Code CLI workers.

    Parameters
    ----------
    session_id:
        NIKA session identifier.
    model:
        Claude model name forwarded to ``claude --model``.  When omitted,
        reads from environment (see :func:`~agent.cli.claude.config.default_claude_model`).
    """

    def __init__(
        self,
        session_id: str,
        model: str | None = None,
        *,
        stream_output: bool = True,
    ) -> None:
        self.session_id = session_id
        self.model = resolve_claude_model(model)
        self._stream_output = stream_output

        session = Session()
        session.load_running_session(session_id=session_id)
        self.session = session
        self.session_dir: str = resolve_agent_session_dir(session.session_dir)

        scenario_name: str = getattr(session, "scenario_name", "")

        self._diagnosis_phase = ClaudeDiagnosisPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=self.model,
            scenario_name=scenario_name,
            stream_output=stream_output,
        )
        self._submission_phase = ClaudeSubmissionPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=self.model,
            stream_output=stream_output,
        )

    async def run(self, task_description: str) -> dict[str, Any]:
        """Execute the two-phase pipeline and return diagnosis + submission results."""
        self._print_phase(DIAGNOSIS, "starting network fault analysis")
        diagnosis_report = await self._diagnosis_phase.run(task_description)
        self._print_phase(
            DIAGNOSIS,
            "completed"
            if not diagnosis_report.startswith("ERROR:")
            else f"finished with error ({diagnosis_report[:120]})",
        )

        self._print_phase(SUBMISSION, "recording structured result")
        submission_result = await self._submission_phase.run(diagnosis_report)
        self._print_phase(SUBMISSION, "completed")

        return {
            "diagnosis_report": diagnosis_report,
            "submission_result": submission_result,
        }

    def _print_phase(self, phase: str, message: str) -> None:
        if not self._stream_output:
            return
        banner = f" [{phase.upper()}] {message} "
        width = max(60, len(banner) + 4)
        print(f"\n{'=' * width}", file=sys.stderr, flush=True)
        print(banner.center(width), file=sys.stderr, flush=True)
        print(f"{'=' * width}\n", file=sys.stderr, flush=True)
