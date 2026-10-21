"""Claude Code CLI troubleshooting agent.

Two-phase pipeline via ``claude -p`` subprocesses (no LangGraph).

* **diagnosis phase** → :class:`~agent.cli.claude.phases.ClaudeDiagnosisPhase`
  (``claude -p`` with Kathara MCP servers; server set chosen dynamically
  based on the session scenario)
* **submission phase** → :class:`~agent.cli.claude.phases.ClaudeSubmissionPhase`
  (``claude -p`` with the task MCP server; calls ``submit()`` to record
  a structured result)

Authentication comes from the sandbox environment the host prepares (see
:mod:`agent.cli.claude.config`).
"""

from __future__ import annotations

from agent.cli.claude.config import resolve_claude_model
from agent.cli.claude.phases.diagnosis import ClaudeDiagnosisPhase
from agent.cli.claude.phases.submission import ClaudeSubmissionPhase
from agent.base import DiagnosisAgent


class ClaudeAgent(DiagnosisAgent):
    """Two-phase troubleshooting agent backed by Claude Code CLI workers.

    Parameters
    ----------
    session_id:
        Opaque session handle of the run.
    model:
        Claude model name forwarded to ``claude --model`` (required).
    llm_provider:
        Active LLM provider (``anthropic``, ``deepseek``, ``custom``).
    max_steps:
        LLM-turn budget per phase (``claude --max-turns``), as for other agents.
    max_tokens:
        Output-token cap per model response (``CLAUDE_CODE_MAX_OUTPUT_TOKENS``).
    """

    def __init__(
        self,
        session_id: str,
        model: str | None = None,
        *,
        llm_provider: str,
        max_steps: int = 20,
        max_tokens: int | None = None,
        stream_output: bool = True,
    ) -> None:
        super().__init__(
            session_id,
            model=resolve_claude_model(model),
            llm_provider=llm_provider,
            max_steps=max_steps,
            max_tokens=max_tokens,
            stream_output=stream_output,
        )
        scenario_name = self.scenario_name

        self._diagnosis_phase = ClaudeDiagnosisPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=self.model,
            llm_provider=llm_provider,
            max_steps=max_steps,
            scenario_name=scenario_name,
            stream_output=stream_output,
            trace_dir=self.trace_dir,
            max_tokens=max_tokens,
        )
        self._submission_phase = ClaudeSubmissionPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=self.model,
            llm_provider=llm_provider,
            max_steps=max_steps,
            stream_output=stream_output,
            trace_dir=self.trace_dir,
            max_tokens=max_tokens,
        )

    async def diagnose(self, task_description: str) -> str:
        return await self._diagnosis_phase.run(task_description)

    async def submit(self, diagnosis_report: str, context: dict) -> str:
        return await self._submission_phase.run(diagnosis_report, context)
