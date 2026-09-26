"""Claude Agent SDK troubleshooting agent.

Two-phase pipeline via native ``ClaudeSDKClient`` sessions (no LangGraph).
Select with ``nika agent run -a sdk.claude_sdk``.
"""

from __future__ import annotations

from agent.sandbox.sdk_context import resolve_sdk_session_fields
from agent.sdk.claude_sdk.config import resolve_claude_sdk_model
from agent.sdk.claude_sdk.phases.diagnosis import ClaudeSdkDiagnosisPhase
from agent.sdk.claude_sdk.phases.submission import ClaudeSdkSubmissionPhase
from agent.utils.two_phase import TwoPhaseAgent


class ClaudeSdkAgent(TwoPhaseAgent):
    """Two-phase troubleshooting agent backed by claude-agent-sdk."""

    def __init__(
        self,
        session_id: str,
        model: str | None = None,
        max_steps: int = 20,
        *,
        llm_provider: str,
        stream_output: bool = True,
    ) -> None:
        self.session_id = session_id
        self.llm_provider = llm_provider
        self.model = resolve_claude_sdk_model(model)
        self.max_steps = max_steps
        self.stream_output = stream_output

        self.session_dir, scenario_name = resolve_sdk_session_fields(session_id)
        self.trace_dir = self.session_dir

        self._diagnosis_phase = ClaudeSdkDiagnosisPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=self.model,
            llm_provider=llm_provider,
            max_steps=max_steps,
            scenario_name=scenario_name,
        )
        self._submission_phase = ClaudeSdkSubmissionPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=self.model,
            llm_provider=llm_provider,
            max_steps=max_steps,
        )

    async def diagnose(self, task_description: str) -> str:
        return await self._diagnosis_phase.run(task_description)

    async def submit(self, diagnosis_report: str, context: dict) -> str:
        return await self._submission_phase.run(diagnosis_report, context)
