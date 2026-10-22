"""Claude Agent SDK troubleshooting agent.

Two-phase pipeline via native ``ClaudeSDKClient`` sessions (no LangGraph).
"""

from __future__ import annotations

from agent.sdk.claude_sdk.config import resolve_claude_sdk_model
from agent.sdk.claude_sdk.phases.diagnosis import ClaudeSdkDiagnosisPhase
from agent.sdk.claude_sdk.phases.submission import ClaudeSdkSubmissionPhase
from agent.base import DiagnosisAgent


class ClaudeSdkAgent(DiagnosisAgent):
    """Two-phase troubleshooting agent backed by claude-agent-sdk."""

    def __init__(
        self,
        session_id: str,
        model: str | None = None,
        max_steps: int = 20,
        *,
        llm_provider: str,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
        stream_output: bool = True,
    ) -> None:
        super().__init__(
            session_id,
            model=resolve_claude_sdk_model(model),
            llm_provider=llm_provider,
            max_steps=max_steps,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            stream_output=stream_output,
        )
        scenario_name = self.scenario_name

        self._diagnosis_phase = ClaudeSdkDiagnosisPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=self.model,
            llm_provider=llm_provider,
            max_steps=max_steps,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            scenario_name=scenario_name,
        )
        self._submission_phase = ClaudeSdkSubmissionPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=self.model,
            llm_provider=llm_provider,
            max_steps=max_steps,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )

    async def diagnose(self, task_description: str) -> str:
        return await self._diagnosis_phase.run(task_description)

    async def submit(self, diagnosis_report: str, context: dict) -> str:
        return await self._submission_phase.run(diagnosis_report, context)
