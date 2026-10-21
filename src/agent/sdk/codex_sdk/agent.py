"""OpenAI Codex SDK troubleshooting agent.

Two-phase pipeline via native ``AsyncCodex`` threads (no LangGraph).
"""

from __future__ import annotations

from agent.sdk.codex_sdk.phases.diagnosis import CodexSdkDiagnosisPhase
from agent.sdk.codex_sdk.phases.submission import CodexSdkSubmissionPhase
from agent.base import DiagnosisAgent


class CodexSdkAgent(DiagnosisAgent):
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
        super().__init__(
            session_id,
            model=model,
            llm_provider=llm_provider,
            max_steps=max_steps,
            reasoning_effort=reasoning_effort,
            stream_output=stream_output,
        )
        scenario_name = self.scenario_name

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
