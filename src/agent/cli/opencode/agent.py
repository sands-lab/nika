"""OpenCode CLI troubleshooting agent.

Two-phase pipeline via ``opencode run`` subprocesses (no LangGraph).

* **diagnosis phase** → :class:`~agent.cli.opencode.phases.OpenCodeDiagnosisPhase`
  (``opencode run`` with diagnosis MCP servers; server set chosen dynamically
  based on the session scenario)
* **submission phase** → :class:`~agent.cli.opencode.phases.OpenCodeSubmissionPhase`
  (``opencode run`` with the task MCP server; calls ``submit()`` to record
  a structured result)
"""

from __future__ import annotations

from agent.cli.opencode.opencode_worker import resolve_opencode_model
from agent.cli.opencode.phases.diagnosis import OpenCodeDiagnosisPhase
from agent.cli.opencode.phases.submission import OpenCodeSubmissionPhase
from agent.base import DiagnosisAgent


class OpenCodeAgent(DiagnosisAgent):
    """Two-phase troubleshooting agent backed by OpenCode CLI workers.

    Parameters
    ----------
    session_id:
        Opaque session handle of the run.
    model:
        Model name forwarded as ``opencode run --model endpoint/<model>``.
    llm_provider:
        Active LLM provider (``openai``, ``anthropic``, ``deepseek``, ``custom``).
    max_steps:
        LLM-turn budget per phase, as for other agents.
    max_tokens:
        Output-token cap per model response, when the CLI honors it.
    reasoning_effort:
        Run reasoning effort, forwarded as ``opencode run --variant``; omitted
        when unset or ``none``.
    """

    def __init__(
        self,
        session_id: str,
        model: str | None = None,
        *,
        llm_provider: str,
        max_steps: int = 20,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        stream_output: bool = True,
    ) -> None:
        super().__init__(
            session_id,
            model=resolve_opencode_model(model),
            llm_provider=llm_provider,
            max_steps=max_steps,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            stream_output=stream_output,
        )
        scenario_name = self.scenario_name

        self._diagnosis_phase = OpenCodeDiagnosisPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=self.model,
            llm_provider=llm_provider,
            max_steps=max_steps,
            scenario_name=scenario_name,
            stream_output=stream_output,
            trace_dir=self.trace_dir,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
        self._submission_phase = OpenCodeSubmissionPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=self.model,
            llm_provider=llm_provider,
            max_steps=max_steps,
            stream_output=stream_output,
            trace_dir=self.trace_dir,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )

    async def diagnose(self, task_description: str) -> str:
        return await self._diagnosis_phase.run(task_description)

    async def submit(self, diagnosis_report: str, context: dict) -> str:
        return await self._submission_phase.run(diagnosis_report, context)
