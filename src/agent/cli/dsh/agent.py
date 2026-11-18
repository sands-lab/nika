"""DeepSeek Harness CLI troubleshooting agent.

Two-phase pipeline via ``dsh --profile headless`` subprocesses (no LangGraph).

* **diagnosis phase** → :class:`~agent.cli.dsh.phases.diagnosis.DshDiagnosisPhase`
  (headless ``dsh`` with diagnosis MCP servers)
* **submission phase** → :class:`~agent.cli.dsh.phases.submission.DshSubmissionPhase`
  (headless ``dsh`` with the task MCP server; calls ``submit()`` to record
  a structured result)
"""

from __future__ import annotations

from agent.base import DiagnosisAgent
from agent.cli.dsh.config import resolve_dsh_model
from agent.cli.dsh.phases.diagnosis import DshDiagnosisPhase
from agent.cli.dsh.phases.submission import DshSubmissionPhase


class DshAgent(DiagnosisAgent):
    """Two-phase troubleshooting agent backed by DeepSeek Harness CLI workers.

    Parameters
    ----------
    session_id:
        Opaque session handle of the run.
    model:
        Model id forwarded through the headless profile (required).
    llm_provider:
        Active LLM provider (``openai``, ``deepseek``, ``custom``).
    max_steps:
        LLM-turn budget per phase, as for other agents.
    max_tokens:
        Output-token cap per model response, when the adapter accepts one.
    reasoning_effort:
        Run reasoning effort, mapped to dsh thinking; omitted when unset
        or ``none``.
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
            model=resolve_dsh_model(model),
            llm_provider=llm_provider,
            max_steps=max_steps,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            stream_output=stream_output,
        )
        scenario_name = self.scenario_name

        self._diagnosis_phase = DshDiagnosisPhase(
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
        self._submission_phase = DshSubmissionPhase(
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
