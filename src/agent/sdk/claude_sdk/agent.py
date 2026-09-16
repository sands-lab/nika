"""Claude Agent SDK troubleshooting agent.

Two-phase pipeline via native ``ClaudeSDKClient`` sessions (no LangGraph).
Select with ``nika agent run -a sdk.claude_sdk``.
"""

from __future__ import annotations

import sys
from typing import Any

from agent.sdk.claude_sdk.config import resolve_claude_sdk_model
from agent.sdk.claude_sdk.phases.diagnosis import ClaudeSdkDiagnosisPhase
from agent.sdk.claude_sdk.phases.submission import ClaudeSdkSubmissionPhase
from agent.sandbox.sdk_context import resolve_sdk_session_fields
from agent.protocols import DIAGNOSIS, SUBMISSION


class ClaudeSdkAgent:
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
        self._stream_output = stream_output

        self.session_dir, scenario_name = resolve_sdk_session_fields(session_id)

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

    async def run(self, task_description: str) -> dict[str, Any]:
        self._print_phase(DIAGNOSIS, "starting network fault analysis")
        diagnosis_report = await self._diagnosis_phase.run(task_description)
        if diagnosis_report.startswith("ERROR:"):
            self._print_phase(DIAGNOSIS, f"failed ({diagnosis_report[:120]})")
            raise RuntimeError(diagnosis_report)
        self._print_phase(DIAGNOSIS, "completed")

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
