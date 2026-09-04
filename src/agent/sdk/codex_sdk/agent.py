"""OpenAI Codex SDK troubleshooting agent.

Two-phase pipeline via native ``AsyncCodex`` threads (no LangGraph).
Select with ``nika agent run -a sdk.codex_sdk``.
"""

from __future__ import annotations

import sys
from typing import Any

from agent.sdk.codex_sdk.phases.diagnosis import CodexSdkDiagnosisPhase
from agent.sdk.codex_sdk.phases.submission import CodexSdkSubmissionPhase
from agent.sandbox.sdk_context import resolve_sdk_session_fields
from agent.protocols import DIAGNOSIS, SUBMISSION


class CodexSdkAgent:
    """Two-phase troubleshooting agent backed by openai-codex."""

    def __init__(
        self,
        session_id: str,
        model: str = "gpt-5.4-mini",
        reasoning_effort: str | None = None,
        *,
        llm_provider: str,
        stream_output: bool = True,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.llm_provider = llm_provider
        self.reasoning_effort = reasoning_effort
        self._stream_output = stream_output

        self.session_dir, scenario_name = resolve_sdk_session_fields(session_id)

        self._diagnosis_phase = CodexSdkDiagnosisPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=model,
            llm_provider=llm_provider,
            reasoning_effort=reasoning_effort,
            scenario_name=scenario_name,
            stream_output=stream_output,
        )
        self._submission_phase = CodexSdkSubmissionPhase(
            session_id=session_id,
            session_dir=self.session_dir,
            model=model,
            llm_provider=llm_provider,
            reasoning_effort=reasoning_effort,
            stream_output=stream_output,
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
