"""AutoGen GraphFlow workflow for the NIKA two-phase troubleshooting pipeline."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from typing import Any

from autogen_agentchat.agents import BaseChatAgent
from autogen_agentchat.base import Response, TaskResult
from autogen_agentchat.messages import BaseChatMessage, TextMessage
from autogen_agentchat.teams import DiGraphBuilder, GraphFlow
from autogen_core import CancellationToken

from agent.byo.autogen.phases.diagnosis import AutogenDiagnosisPhase
from agent.byo.autogen.phases.submission import AutogenSubmissionPhase
from agent.utils.loggers import MessageLogger
from agent.protocols import DIAGNOSIS, SUBMISSION

_MAX_STEPS_MARKER = "ERROR_MAX_STEPS_REACHED"


class DiagnosisPhaseAgent(BaseChatAgent):
    """GraphFlow node that runs the diagnosis phase."""

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
        print_phase: Callable[[str, str], None],
    ) -> None:
        super().__init__(
            name=DIAGNOSIS,
            description="Network fault diagnosis using Kathara MCP tools.",
        )
        self._phase = AutogenDiagnosisPhase(
            session_id=session_id,
            session_dir=session_dir,
            model=model,
            max_steps=max_steps,
            scenario_name=scenario_name,
            llm_provider=llm_provider,
            reasoning_effort=reasoning_effort,
        )
        self._session_dir = session_dir
        self._print_phase = print_phase

    @property
    def produced_message_types(self) -> Sequence[type[BaseChatMessage]]:
        return (TextMessage,)

    async def on_reset(self, cancellation_token: CancellationToken) -> None:
        return None

    async def on_messages(
        self,
        messages: Sequence[BaseChatMessage],
        cancellation_token: CancellationToken,
    ) -> Response:
        task_description = messages[-1].content if messages else ""
        if not isinstance(task_description, str):
            task_description = str(task_description)

        logger = MessageLogger(agent=DIAGNOSIS, session_dir=self._session_dir)
        self._print_phase(DIAGNOSIS, "starting network fault analysis")
        logger.log(
            "agent_start", {"phase": DIAGNOSIS, "task_preview": task_description[:200]}
        )

        try:
            report, is_max_steps_reached = await self._phase.run(task_description)
        except Exception as exc:
            logger.log("agent_error", {"phase": DIAGNOSIS, "error": str(exc)})
            report = f"ERROR: {exc}"
            is_max_steps_reached = False

        if is_max_steps_reached:
            logger.log(
                "error", {"message": "Diagnosis phase reached max iteration limit."}
            )
            logger.log(
                "agent_done",
                {"phase": DIAGNOSIS, "is_error": True, "report_length": len(report)},
            )
            self._print_phase(DIAGNOSIS, "stopped: max steps reached")
            report = _MAX_STEPS_MARKER
        else:
            is_error = report.startswith("ERROR:")
            logger.log(
                "agent_done",
                {
                    "phase": DIAGNOSIS,
                    "is_error": is_error,
                    "report_length": len(report),
                },
            )
            self._print_phase(
                DIAGNOSIS,
                "completed"
                if not is_error
                else f"finished with error ({report[:120]})",
            )

        return Response(chat_message=TextMessage(content=report, source=self.name))

    async def on_messages_stream(self, messages, cancellation_token):
        response = await self.on_messages(messages, cancellation_token)
        yield response


class SubmissionPhaseAgent(BaseChatAgent):
    """GraphFlow node that runs the submission phase."""

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
        print_phase: Callable[[str, str], None],
    ) -> None:
        super().__init__(
            name=SUBMISSION,
            description="Structured submission via the task MCP server.",
        )
        self._phase = AutogenSubmissionPhase(
            session_id=session_id,
            session_dir=session_dir,
            model=model,
            max_steps=max_steps,
            scenario_name=scenario_name,
            llm_provider=llm_provider,
            reasoning_effort=reasoning_effort,
        )
        self._session_dir = session_dir
        self._print_phase = print_phase

    @property
    def produced_message_types(self) -> Sequence[type[BaseChatMessage]]:
        return (TextMessage,)

    async def on_reset(self, cancellation_token: CancellationToken) -> None:
        return None

    async def on_messages(
        self,
        messages: Sequence[BaseChatMessage],
        cancellation_token: CancellationToken,
    ) -> Response:
        diagnosis_report = messages[-1].content if messages else ""
        if not isinstance(diagnosis_report, str):
            diagnosis_report = str(diagnosis_report)

        logger = MessageLogger(agent=SUBMISSION, session_dir=self._session_dir)
        self._print_phase(SUBMISSION, "recording structured result")
        logger.log("agent_start", {"phase": SUBMISSION})

        try:
            result = await self._phase.run(diagnosis_report)
        except Exception as exc:
            logger.log("agent_error", {"phase": SUBMISSION, "error": str(exc)})
            result = ""

        logger.log("agent_done", {"phase": SUBMISSION, "result_length": len(result)})
        self._print_phase(SUBMISSION, "completed")
        return Response(chat_message=TextMessage(content=result, source=self.name))

    async def on_messages_stream(self, messages, cancellation_token):
        response = await self.on_messages(messages, cancellation_token)
        yield response


def _should_submit(message: BaseChatMessage) -> bool:
    return _MAX_STEPS_MARKER not in message.to_model_text()


async def run_troubleshooting_flow(
    *,
    task_description: str,
    session_id: str,
    session_dir: str,
    model: str,
    max_steps: int,
    scenario_name: str,
    stream_output: bool,
    llm_provider: str,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """Execute diagnosis → submission via AutoGen ``GraphFlow``."""
    print_phase = _make_print_phase(stream_output)

    diagnosis_agent = DiagnosisPhaseAgent(
        session_id=session_id,
        session_dir=session_dir,
        model=model,
        max_steps=max_steps,
        scenario_name=scenario_name,
        llm_provider=llm_provider,
        reasoning_effort=reasoning_effort,
        print_phase=print_phase,
    )
    submission_agent = SubmissionPhaseAgent(
        session_id=session_id,
        session_dir=session_dir,
        model=model,
        max_steps=max_steps,
        scenario_name=scenario_name,
        llm_provider=llm_provider,
        reasoning_effort=reasoning_effort,
        print_phase=print_phase,
    )

    builder = DiGraphBuilder()
    builder.add_node(diagnosis_agent).add_node(submission_agent)
    builder.add_edge(diagnosis_agent, submission_agent, condition=_should_submit)
    builder.set_entry_point(diagnosis_agent)

    team = GraphFlow(
        participants=[diagnosis_agent, submission_agent],
        graph=builder.build(),
        max_turns=3,
    )
    result: TaskResult = await team.run(task=task_description)
    return _result_to_state(result)


def _result_to_state(result: TaskResult) -> dict[str, Any]:
    diagnosis_report = ""
    submission_result = ""
    is_max_steps_reached = False

    for message in result.messages:
        if not isinstance(message, TextMessage):
            continue
        if message.source == DIAGNOSIS:
            diagnosis_report = message.content
            is_max_steps_reached = diagnosis_report == _MAX_STEPS_MARKER
        elif message.source == SUBMISSION:
            submission_result = message.content

    return {
        "diagnosis_report": diagnosis_report,
        "is_max_steps_reached": is_max_steps_reached,
        "submission_result": submission_result,
    }


def _make_print_phase(stream_output: bool):
    def print_phase(phase: str, message: str) -> None:
        if not stream_output:
            return
        banner = f" [{phase.upper()}] {message} "
        width = max(60, len(banner) + 4)
        print(f"\n{'=' * width}", file=sys.stderr, flush=True)
        print(banner.center(width), file=sys.stderr, flush=True)
        print(f"{'=' * width}\n", file=sys.stderr, flush=True)

    return print_phase
