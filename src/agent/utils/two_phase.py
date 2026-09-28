"""Shared diagnosis -> freeze -> submission pipeline for troubleshooting agents.

Every agent runs the same contract:

* **Diagnosis** uses at most ``max_steps`` LLM turns (model responses). When
  the budget runs out, the latest assistant text becomes the report
  (:func:`max_steps_report`). A report that starts with ``ERROR:`` fails the
  run with :class:`RuntimeError`, and submission never starts.
* **Freeze and advance** happen on the host (:func:`begin_submission_mcp_phase`),
  which returns the submission context (fault ontology + resource catalog).
* **Submission** gets the frozen report and context and calls ``submit()``.

Each phase writes ``agent_start`` then ``agent_done`` or ``agent_error`` to
``messages.jsonl``; workers must not write these bookends themselves.
"""

from __future__ import annotations

import sys
from typing import Any

from agent.protocols import DIAGNOSIS, SUBMISSION
from agent.utils.loggers import MessageLogger

ERROR_PREFIX = "ERROR:"


def max_steps_report(logger: MessageLogger, *, max_steps: int, latest_text: str) -> str:
    """Report for a phase that used its whole ``max_steps`` budget.

    Returns the latest assistant text; with none, an ``ERROR:`` report so the
    pipeline fails the same way for every agent.
    """
    logger.log("max_steps_reached", {"max_steps": max_steps})
    text = (latest_text or "").strip()
    if text:
        return text
    return (
        f"{ERROR_PREFIX} {logger.phase} phase reached max_steps ({max_steps}) "
        "without an assistant report"
    )


class TwoPhaseAgent:
    """Base class: subclasses implement :meth:`diagnose` and :meth:`submit`.

    Subclasses set ``session_id``, ``trace_dir`` (host directory that holds
    ``messages.jsonl``), and ``stream_output``.
    """

    session_id: str
    trace_dir: str
    stream_output: bool = True

    async def diagnose(self, task_description: str) -> str:
        """Return the diagnosis report (``ERROR: ...`` on failure)."""
        raise NotImplementedError

    async def submit(self, diagnosis_report: str, context: dict[str, Any]) -> str:
        """Run the submission phase against the frozen report and context."""
        raise NotImplementedError

    async def run_diagnosis(self, task_description: str) -> str:
        logger = MessageLogger(phase=DIAGNOSIS, session_dir=self.trace_dir)
        self.print_phase(DIAGNOSIS, "starting network fault analysis")
        logger.log_agent_start()
        try:
            report = await self.diagnose(task_description)
        except BaseException as exc:
            logger.log_agent_error(exc)
            self.print_phase(DIAGNOSIS, f"failed ({str(exc)[:120]})")
            raise
        if report.startswith(ERROR_PREFIX):
            logger.log_agent_error(report)
            self.print_phase(DIAGNOSIS, f"failed ({report[:120]})")
            raise RuntimeError(report)
        logger.log_agent_done(report_length=len(report))
        self.print_phase(DIAGNOSIS, "completed")
        return report

    async def run_submission(
        self, diagnosis_report: str, context: dict[str, Any]
    ) -> str:
        logger = MessageLogger(phase=SUBMISSION, session_dir=self.trace_dir)
        self.print_phase(SUBMISSION, "recording structured result")
        logger.log_agent_start()
        try:
            result = await self.submit(diagnosis_report, context)
        except BaseException as exc:
            logger.log_agent_error(exc)
            self.print_phase(SUBMISSION, f"failed ({str(exc)[:120]})")
            raise
        if result.startswith(ERROR_PREFIX):
            # submit() may already have recorded the answer; keep the run going.
            logger.log_agent_error(result)
            self.print_phase(SUBMISSION, f"failed ({result[:120]})")
        else:
            logger.log_agent_done(result_length=len(result))
            self.print_phase(SUBMISSION, "completed")
        return result

    async def run(self, task_description: str) -> dict[str, Any]:
        from agent.utils.mcp_client import begin_submission_mcp_phase

        diagnosis_report = await self.run_diagnosis(task_description)
        context = begin_submission_mcp_phase(self.session_id, diagnosis_report)
        submission_result = await self.run_submission(diagnosis_report, context)
        return {
            "diagnosis_report": diagnosis_report,
            "submission_result": submission_result,
        }

    def print_phase(self, phase: str, message: str) -> None:
        if not self.stream_output:
            return
        banner = f" [{phase.upper()}] {message} "
        width = max(60, len(banner) + 4)
        print(f"\n{'=' * width}", file=sys.stderr, flush=True)
        print(banner.center(width), file=sys.stderr, flush=True)
        print(f"{'=' * width}\n", file=sys.stderr, flush=True)
