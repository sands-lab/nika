"""Codex CLI troubleshooting agent.

Two-phase pipeline via ``codex exec`` subprocesses (no LangGraph).

* **diagnosis phase** → :class:`~agent.cli.codex.phases.CodexCliDiagnosisPhase`
  (``codex exec`` with Kathara MCP servers; server set chosen dynamically
  based on the session scenario)
* **submission phase** → :class:`~agent.cli.codex.phases.CodexCliSubmissionPhase`
  (``codex exec`` with the task MCP server; calls ``submit()`` to record
  a structured result)

Session ID propagation follows the same path as the LangChain path:
``NIKA_SESSION_ID`` is injected into each MCP server's ``env`` block via
:class:`~agent.utils.mcp_servers.MCPServerConfig`.

Select with ``nika agent run -a cli.codex``.
"""

from __future__ import annotations

import sys
from typing import Any

from agent.cli.codex.phases.diagnosis import CodexCliDiagnosisPhase
from agent.cli.codex.phases.submission import CodexCliSubmissionPhase
from agent.sandbox.session_dir import resolve_agent_session_dir
from agent.protocols import DIAGNOSIS, SUBMISSION
from nika.utils.session import Session


class CodexCliAgent:
    """Two-phase troubleshooting agent backed by Codex CLI workers.

    Parameters
    ----------
    session_id:
        NIKA session identifier.
    model:
        Codex model name forwarded to ``codex exec -m`` (default ``"gpt-5.4-mini"``).
    llm_provider:
        Active LLM provider (``openai``, ``deepseek``, ``custom``).
    reasoning_effort:
        Codex ``model_reasoning_effort`` override (``none``, ``minimal``, ``low``,
        ``medium``, ``high``, ``xhigh``).  When omitted, Codex uses its default.
    """

    def __init__(
        self,
        session_id: str,
        model: str = "gpt-5.4-mini",
        reasoning_effort: str | None = None,
        *,
        llm_provider: str,
        stream_output: bool = True,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.llm_provider = llm_provider
        self.reasoning_effort = reasoning_effort
        self._stream_output = stream_output

        session = Session()
        session.load_running_session(session_id=session_id)
        self.session = session
        self.session_dir: str = resolve_agent_session_dir(session.session_dir)

        scenario_name: str = getattr(session, "scenario_name", "")

        self._diagnosis_phase = CodexCliDiagnosisPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=model,
            llm_provider=llm_provider,
            reasoning_effort=reasoning_effort,
            scenario_name=scenario_name,
            stream_output=stream_output,
        )
        self._submission_phase = CodexCliSubmissionPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=model,
            llm_provider=llm_provider,
            reasoning_effort=reasoning_effort,
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
