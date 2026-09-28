"""OpenAI Codex SDK troubleshooting agent.

Two-phase pipeline via native ``AsyncCodex`` threads (no LangGraph).
Select with ``nika agent run -a sdk.codex_sdk``.
"""

from __future__ import annotations

from agent.sandbox.sdk_context import resolve_sdk_session_fields
from agent.sdk.codex_sdk.phases.diagnosis import CodexSdkDiagnosisPhase
from agent.sdk.codex_sdk.phases.submission import CodexSdkSubmissionPhase
from agent.utils.two_phase import TwoPhaseAgent


class CodexSdkAgent(TwoPhaseAgent):
    """Two-phase troubleshooting agent backed by openai-codex."""

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
        self.session_id = session_id
        self.model = model
        self.llm_provider = llm_provider
        self.reasoning_effort = reasoning_effort
        self.stream_output = stream_output

        self.session_dir, scenario_name = resolve_sdk_session_fields(session_id)
        self.trace_dir = self.session_dir

        self._diagnosis_phase = CodexSdkDiagnosisPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=model,
            llm_provider=llm_provider,
            reasoning_effort=reasoning_effort,
            scenario_name=scenario_name,
            max_steps=max_steps,
            stream_output=stream_output,
        )
        self._submission_phase = CodexSdkSubmissionPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=model,
            llm_provider=llm_provider,
            reasoning_effort=reasoning_effort,
            max_steps=max_steps,
            stream_output=stream_output,
        )

    async def diagnose(self, task_description: str) -> str:
        return await self._diagnosis_phase.run(task_description)

    async def submit(self, diagnosis_report: str, context: dict) -> str:
        return await self._submission_phase.run(diagnosis_report, context)
