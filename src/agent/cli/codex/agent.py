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

from agent.cli.codex.phases.diagnosis import CodexCliDiagnosisPhase
from agent.cli.codex.phases.submission import CodexCliSubmissionPhase
from agent.base import NikaAgent


class CodexCliAgent(NikaAgent):
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
    max_steps:
        LLM-turn budget per phase, as for other agents.
    """

    def __init__(
        self,
        session_id: str,
        model: str = "gpt-5.4-mini",
        reasoning_effort: str | None = None,
        max_steps: int = 20,
        *,
        llm_provider: str,
        stream_output: bool = True,
    ) -> None:
        # The sandbox workspace holds the CLI workspace; the trace stays in the
        # host session dir, which the sandbox cannot write.
        super().__init__(
            session_id,
            model=model,
            llm_provider=llm_provider,
            max_steps=max_steps,
            reasoning_effort=reasoning_effort,
            stream_output=stream_output,
        )
        scenario_name = self.scenario_name

        self._diagnosis_phase = CodexCliDiagnosisPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=model,
            llm_provider=llm_provider,
            reasoning_effort=reasoning_effort,
            max_steps=max_steps,
            scenario_name=scenario_name,
            stream_output=stream_output,
            trace_dir=self.trace_dir,
        )
        self._submission_phase = CodexCliSubmissionPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=model,
            llm_provider=llm_provider,
            reasoning_effort=reasoning_effort,
            max_steps=max_steps,
            stream_output=stream_output,
            trace_dir=self.trace_dir,
        )

    async def diagnose(self, task_description: str) -> str:
        return await self._diagnosis_phase.run(task_description)

    async def submit(self, diagnosis_report: str, context: dict) -> str:
        return await self._submission_phase.run(diagnosis_report, context)
