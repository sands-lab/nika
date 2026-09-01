"""mcp-agent Workflow for the NIKA two-phase troubleshooting pipeline."""

from __future__ import annotations

import sys
from typing import Any

import agent.byo.mcp_agent._bootstrap  # noqa: F401

from mcp_agent.executor.workflow import Workflow, WorkflowResult

from agent.byo.mcp_agent.config import session_server_names
from agent.byo.mcp_agent.phases.diagnosis import McpDiagnosisPhase
from agent.byo.mcp_agent.phases.submission import McpSubmissionPhase
from agent.utils.loggers import MessageLogger
from agent.protocols import DIAGNOSIS, SUBMISSION


class NikaTroubleshootingWorkflow(Workflow[dict[str, Any]]):
    """Diagnosis → submission pipeline using mcp-agent ``Workflow``."""

    def __init__(
        self,
        *,
        session_id: str,
        session_dir: str,
        model: str,
        max_steps: int,
        scenario_name: str,
        llm_provider: str,
        reasoning_effort: str | None = None,
        stream_output: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(name="nika_troubleshooting", **kwargs)
        self._session_id = session_id
        self._session_dir = session_dir
        self._model = model
        self._max_steps = max_steps
        self._scenario_name = scenario_name
        self._llm_provider = llm_provider
        self._reasoning_effort = reasoning_effort
        self._stream_output = stream_output
        self._server_names = session_server_names(scenario_name)

    async def run(self, task_description: str) -> WorkflowResult[dict[str, Any]]:
        diagnosis_report, is_max_steps_reached = await self._run_diagnosis(
            task_description
        )
        if is_max_steps_reached:
            return WorkflowResult(
                value={
                    "diagnosis_report": "ERROR_MAX_STEPS_REACHED",
                    "is_max_steps_reached": True,
                }
            )

        submission_result = await self._run_submission(diagnosis_report)
        return WorkflowResult(
            value={
                "diagnosis_report": diagnosis_report,
                "is_max_steps_reached": False,
                "submission_result": submission_result,
            }
        )

    async def _run_diagnosis(self, task_description: str) -> tuple[str, bool]:
        logger = MessageLogger(phase=DIAGNOSIS, session_dir=self._session_dir)
        self._print_phase(DIAGNOSIS, "starting network fault analysis")
        logger.log(
            "agent_start", {"phase": DIAGNOSIS, "task_preview": task_description[:200]}
        )

        phase = McpDiagnosisPhase(
            session_dir=self._session_dir,
            model=self._model,
            max_steps=self._max_steps,
            server_names=self._server_names,
            llm_provider=self._llm_provider,
            reasoning_effort=self._reasoning_effort,
        )
        try:
            report, is_max_steps_reached = await phase.run(task_description)
        except Exception as exc:
            logger.log("agent_error", {"phase": DIAGNOSIS, "error": str(exc)})
            return f"ERROR: {exc}", False

        if is_max_steps_reached:
            logger.log(
                "error", {"message": "Diagnosis phase reached max iteration limit."}
            )
            logger.log(
                "agent_done",
                {"phase": DIAGNOSIS, "is_error": True, "report_length": len(report)},
            )
            self._print_phase(DIAGNOSIS, "stopped: max steps reached")
            return report, True

        is_error = report.startswith("ERROR:")
        logger.log(
            "agent_done",
            {"phase": DIAGNOSIS, "is_error": is_error, "report_length": len(report)},
        )
        self._print_phase(
            DIAGNOSIS,
            "completed" if not is_error else f"finished with error ({report[:120]})",
        )
        return report, False

    async def _run_submission(self, diagnosis_report: str) -> str:
        logger = MessageLogger(phase=SUBMISSION, session_dir=self._session_dir)
        self._print_phase(SUBMISSION, "recording structured result")
        logger.log("agent_start", {"phase": SUBMISSION})

        phase = McpSubmissionPhase(
            session_id=self._session_id,
            session_dir=self._session_dir,
            model=self._model,
            max_steps=self._max_steps,
            server_names=self._server_names,
            llm_provider=self._llm_provider,
            reasoning_effort=self._reasoning_effort,
        )
        try:
            result = await phase.run(diagnosis_report)
        except Exception as exc:
            logger.log("agent_error", {"phase": SUBMISSION, "error": str(exc)})
            return ""

        logger.log("agent_done", {"phase": SUBMISSION, "result_length": len(result)})
        self._print_phase(SUBMISSION, "completed")
        return result

    def _print_phase(self, phase: str, message: str) -> None:
        if not self._stream_output:
            return
        banner = f" [{phase.upper()}] {message} "
        width = max(60, len(banner) + 4)
        print(f"\n{'=' * width}", file=sys.stderr, flush=True)
        print(banner.center(width), file=sys.stderr, flush=True)
        print(f"{'=' * width}\n", file=sys.stderr, flush=True)
