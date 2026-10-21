"""Claude Code CLI troubleshooting agent.

Two-phase pipeline via ``claude -p`` subprocesses (no LangGraph).

* **diagnosis phase** → :class:`~agent.cli.claude.phases.ClaudeDiagnosisPhase`
  (``claude -p`` with Kathara MCP servers; server set chosen dynamically
  based on the session scenario)
* **submission phase** → :class:`~agent.cli.claude.phases.ClaudeSubmissionPhase`
  (``claude -p`` with the task MCP server; calls ``submit()`` to record
  a structured result)

Authentication uses ``agent.provider`` (``-p`` / config/nika.yaml) with
``ANTHROPIC_API_KEY``, ``DEEPSEEK_API_KEY``, or ``NIKA_CUSTOM_*`` (mapped for
the subprocess only), or ``claude auth login``.  See
:mod:`agent.cli.claude.config` and ``docs/agents/agent-implementations.md``.

Select with ``nika agent run -a cli.claude``.
"""

from __future__ import annotations

from agent.cli.claude.config import resolve_claude_model
from agent.cli.claude.phases.diagnosis import ClaudeDiagnosisPhase
from agent.cli.claude.phases.submission import ClaudeSubmissionPhase
from agent.base import NikaAgent


class ClaudeAgent(NikaAgent):
    """Two-phase troubleshooting agent backed by Claude Code CLI workers.

    Parameters
    ----------
    session_id:
        NIKA session identifier.
    model:
        Claude model name forwarded to ``claude --model``.  When omitted,
        requires ``agent.model`` / ``-m`` (see
        :func:`~agent.cli.claude.config.resolve_claude_model`).
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
        # The sandbox workspace holds the CLI workspace; the trace stays in the
        # host session dir, which the sandbox cannot write.
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
