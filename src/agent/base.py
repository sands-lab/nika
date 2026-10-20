"""Base class for NIKA agents: diagnosis -> freeze -> submission.

An agent implements :meth:`NikaAgent.diagnose`; NIKA supplies the rest:

* **Setup**: the constructor loads the running session and keeps the run
  settings (``model``, ``llm_provider``, ``max_steps`` ...).
* **Diagnosis** uses at most ``max_steps`` LLM turns (model responses). When
  the budget runs out, the latest assistant text becomes the report
  (:func:`max_steps_report`). An empty report, or one that starts with
  ``ERROR:``, fails the run with :class:`RuntimeError`, and submission never
  starts.
* **Freeze and advance** happen on the host (:func:`begin_submission_mcp_phase`),
  which returns the submission context (fault ontology + resource catalog).
* **Submission** turns the frozen report into the scored answer. In the
  default ``structured`` mode the agent's own model calls the task MCP
  ``submit`` tool (:meth:`NikaAgent.submit`, which harnesses may override).
  In ``unstructured`` mode a NIKA :class:`~agent.submission.ReportMapper`
  answers instead, for every agent type.

Each phase writes ``agent_start`` then ``agent_done`` or ``agent_error`` to
``messages.jsonl``; workers must not write these bookends themselves.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from agent.protocols import DIAGNOSIS, SUBMISSION
from agent.utils.display import agent_output
from agent.utils.loggers import MessageLogger

if TYPE_CHECKING:
    from agent.submission import ReportMapper

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


def print_phase(stream_output: bool, phase: str, message: str) -> None:
    if not stream_output:
        return
    banner = f" [{phase.upper()}] {message} "
    width = max(60, len(banner) + 4)
    print(f"\n{'=' * width}", file=sys.stderr, flush=True)
    print(banner.center(width), file=sys.stderr, flush=True)
    print(f"{'=' * width}\n", file=sys.stderr, flush=True)


async def run_submission_phase(
    answer: Callable[[], Awaitable[str]],
    *,
    trace_dir: str,
    stream_output: bool,
    **start_fields: Any,
) -> str:
    """Run one submission attempt between the phase bookends.

    Shared by :meth:`NikaAgent.run_submission` and the sandbox runner, which
    maps the report on the host for sandboxed agents.
    """
    logger = MessageLogger(phase=SUBMISSION, session_dir=trace_dir)
    print_phase(stream_output, SUBMISSION, "recording structured result")
    logger.log_agent_start(**start_fields)
    try:
        with agent_output(stream_output):
            result = await answer()
    except BaseException as exc:
        logger.log_agent_error(exc)
        print_phase(stream_output, SUBMISSION, f"failed ({str(exc)[:120]})")
        raise
    if result.startswith(ERROR_PREFIX):
        # The answer may already be recorded; keep the run going.
        logger.log_agent_error(result)
        print_phase(stream_output, SUBMISSION, f"failed ({result[:120]})")
    else:
        logger.log_agent_done(result_length=len(result))
        print_phase(stream_output, SUBMISSION, "completed")
    return result


def _session_paths(session_id: str) -> tuple[Any, str, str, str]:
    """Return ``(session, session_dir, trace_dir, scenario_name)``.

    Inside a sandbox the runner has no session store, so the workspace and
    its manifest stand in (``session`` is ``None``). Host-side CLI agents work
    in the sandbox workspace but trace to the host session directory.
    """
    from agent.sandbox.config import ENV_SANDBOX_EXECUTION, sandbox_name_from_env

    if os.environ.get(ENV_SANDBOX_EXECUTION) == "1" and not sandbox_name_from_env():
        from agent.sandbox.sdk_context import resolve_sdk_session_fields

        session_dir, scenario_name = resolve_sdk_session_fields(session_id)
        return None, session_dir, session_dir, scenario_name

    from agent.sandbox.session_dir import (
        resolve_agent_session_dir,
        resolve_agent_trace_dir,
    )
    from nika.utils.session import Session

    session = Session()
    session.load_running_session(session_id=session_id)
    return (
        session,
        resolve_agent_session_dir(session.session_dir),
        resolve_agent_trace_dir(session_id, session.session_dir),
        str(getattr(session, "scenario_name", "") or ""),
    )


class NikaAgent:
    """Base class for NIKA agents; subclasses implement :meth:`diagnose`.

    NIKA constructs the agent with the run settings below (each subclass
    receives the ones its constructor declares). Attributes available to
    subclasses: ``session_id``, ``session`` (``None`` inside a sandbox),
    ``session_dir`` (agent workspace), ``trace_dir`` (holds
    ``messages.jsonl``), ``scenario_name``, and every setting.
    """

    # Set by NIKA for ``agent.submission.mode: unstructured``.
    report_mapper: ReportMapper | None = None

    def __init__(
        self,
        session_id: str,
        model: str | None = None,
        llm_provider: str | None = None,
        max_steps: int = 20,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
        stream_output: bool = True,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.llm_provider = llm_provider
        self.max_steps = max_steps
        self.reasoning_effort = reasoning_effort
        self.max_tokens = max_tokens
        self.stream_output = stream_output
        self.session, self.session_dir, self.trace_dir, self.scenario_name = (
            _session_paths(session_id)
        )

    async def diagnose(self, task_description: str) -> str:
        """Investigate the network and return the diagnosis report.

        Return ``ERROR: ...`` (or raise) when the investigation fails.
        """
        raise NotImplementedError

    async def submit(self, diagnosis_report: str, context: dict[str, Any]) -> str:
        """Answer from the frozen report with the agent's own model.

        The default runs NIKA's submission agent: the model sees only the
        report and *context* and calls the task MCP ``submit`` tool.
        Harnesses with their own submission worker override this.
        """
        from agent.submission import submit_with_agent_model

        return await submit_with_agent_model(self, diagnosis_report, context)

    def diagnosis_mcp_config(self) -> dict[str, Any]:
        """HTTP MCP servers available during diagnosis, keyed by server name."""
        from agent.utils.mcp_client import load_session_mcp_config

        return load_session_mcp_config(
            self.session_id, self.scenario_name, phase=DIAGNOSIS
        )

    async def run_diagnosis(self, task_description: str) -> str:
        logger = MessageLogger(phase=DIAGNOSIS, session_dir=self.trace_dir)
        self.print_phase(DIAGNOSIS, "starting network fault analysis")
        logger.log_agent_start()
        try:
            with agent_output(self.stream_output):
                report = await self.diagnose(task_description)
        except BaseException as exc:
            logger.log_agent_error(exc)
            self.print_phase(DIAGNOSIS, f"failed ({str(exc)[:120]})")
            raise
        report = report or ""
        if not report.strip():
            # Never freeze an empty report: submission would run on nothing.
            report = f"{ERROR_PREFIX} diagnosis phase produced an empty report"
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
        mapper = self.report_mapper
        if mapper is None:
            return await run_submission_phase(
                lambda: self.submit(diagnosis_report, context),
                trace_dir=self.trace_dir,
                stream_output=self.stream_output,
                submission_mode="structured",
            )
        return await run_submission_phase(
            lambda: mapper.submit(
                diagnosis_report,
                context,
                session_id=self.session_id,
                scenario_name=self.scenario_name,
                trace_dir=self.trace_dir,
            ),
            trace_dir=self.trace_dir,
            stream_output=self.stream_output,
            **mapper.trace_fields(),
        )

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
        print_phase(self.stream_output, phase, message)
