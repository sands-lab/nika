import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from pydantic import Field, ValidationError
from typing_extensions import TypedDict

from agent.byo.langgraph.phases.diagnosis import DiagnosisPhase
from agent.byo.langgraph.phases.submission import SubmissionPhase
from agent.utils.loggers import AgentCallbackLogger, MessageLogger, MESSAGES_FILENAME
from agent.utils.mcp_client import begin_submission_mcp_phase
from agent.utils.submission_context import submission_prompt_context
from agent.protocols import DIAGNOSIS, SUBMISSION
from nika.utils.logger import system_logger
from nika.utils.session import Session

load_dotenv()


logging.basicConfig(level=logging.INFO)


class AgentState(TypedDict):
    """The state of the agent."""

    messages: list[BaseMessage]
    diagnosis_report: str = Field(
        default="",
        description="The diagnosis report of the network state after analysis.",
    )
    is_max_steps_reached: bool = Field(
        default=False,
        description="Indicates whether the agent has reached the maximum number of steps allowed.",
    )


class BasicReActAgent:
    def __init__(
        self,
        session_id: str,
        llm_provider: str = "openai",
        model: str = "gpt-5-mini",
        max_steps: int = 20,
        reasoning_effort: str | None = None,
    ):
        self.session_id = session_id
        self.max_steps = max_steps
        self.llm_provider = llm_provider
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.session = Session()
        self.session.load_running_session(session_id=session_id)
        self.session_dir = self.session.session_dir

        self.langfuse_handler = self._load_langfuse_handler()

        diagnosis_phase = DiagnosisPhase(
            session_id=session_id,
            llm_provider=llm_provider,
            model=model,
            scenario_name=self.session.scenario_name,
            reasoning_effort=reasoning_effort,
        )
        asyncio.run(diagnosis_phase.load_tools())
        self._diagnosis_runner = diagnosis_phase.get_agent()

        worker_builder = StateGraph(AgentState)
        worker_builder.add_node(DIAGNOSIS, self._run_diagnosis)
        worker_builder.add_node(SUBMISSION, self._run_submission)

        worker_builder.add_edge(START, DIAGNOSIS)
        # Submission is the agent's terminal action even if diagnosis reaches
        # its tool-budget.  The frozen report then records that limitation.
        worker_builder.add_edge(DIAGNOSIS, SUBMISSION)
        worker_builder.add_edge(SUBMISSION, END)

        # compile the graph
        self.graph = worker_builder.compile()

    async def run(self, task_description: str):
        callbacks: list[Any] = []
        if self.langfuse_handler is not None:
            callbacks.append(self.langfuse_handler)

        result = await self.graph.ainvoke(
            {
                "messages": [HumanMessage(content=task_description)],
            },
            config={"callbacks": callbacks},
        )
        return result

    def _load_langfuse_handler(self) -> Any | None:
        enabled = False
        try:
            from nika.run_config.loader import get_run_config

            obs = get_run_config().nika.observability
            enabled = bool(obs.langfuse_enabled)
            if obs.langfuse_host:
                os.environ.setdefault("LANGFUSE_HOST", obs.langfuse_host)
        except Exception:
            enabled = False
        if not enabled:
            return None

        try:
            from langfuse import get_client
            from langfuse.langchain import CallbackHandler
        except ImportError as exc:
            raise RuntimeError(
                "Observability langfuse is enabled, but langfuse is not installed. "
                "Install the observability extra or set nika.observability.langfuse_enabled: false."
            ) from exc

        langfuse = get_client()
        handler = CallbackHandler()

        if langfuse.auth_check():
            system_logger.info("Authentication to Langfuse successful.")
        else:
            system_logger.warning(
                "Authentication to Langfuse failed. Please check your LANGFUSE_API_KEY."
            )
        return handler

    async def _run_diagnosis(self, state: AgentState):
        try:
            cb = AgentCallbackLogger(phase=DIAGNOSIS, session_dir=self.session_dir)
            diagnosis_report = await self._diagnosis_runner.ainvoke(
                {"messages": state["messages"]},
                config={
                    "callbacks": [cb],
                    "recursion_limit": self.max_steps,
                },
                debug=True,
            )
            return {
                "diagnosis_report": [diagnosis_report["messages"][-1].content],
                "is_max_steps_reached": False,
            }
        except ValidationError as e:
            MessageLogger(phase=DIAGNOSIS, session_dir=self.session_dir).log(
                "error", {"message": f"Validation error: {e}"}
            )
            return {
                "messages": [HumanMessage(content=f"Error: {e}")],
                "diagnosis_report": ["ERROR_VALIDATION"],
                "is_max_steps_reached": False,
            }
        except GraphRecursionError:
            MessageLogger(phase=DIAGNOSIS, session_dir=self.session_dir).log(
                "error",
                {"message": "Diagnosis phase reached max recursion limit."},
            )
            return {
                "messages": [],
                "diagnosis_report": [self._recent_diagnosis_report()],
                "is_max_steps_reached": True,
            }

    def _recent_diagnosis_report(self) -> str:
        """Preserve the latest agent conclusions when its diagnosis budget expires."""
        path = Path(self.session_dir) / MESSAGES_FILENAME
        conclusions: list[str] = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("phase") == DIAGNOSIS and event.get("event") == "llm_end":
                    text = event.get("text")
                    if isinstance(text, str) and text.strip():
                        conclusions.append(text.strip())
        suffix = "\n\n".join(conclusions[-5:])
        return (
            "Diagnosis reached its tool budget; the following frozen conclusions "
            "are the available evidence:\n\n" + suffix
        )

    async def _run_submission(self, state: AgentState):
        begin_submission_mcp_phase(self.session_id, str(state["diagnosis_report"][-1]))
        submission_phase = SubmissionPhase(
            session_id=self.session_id,
            llm_provider=self.llm_provider,
            model=self.model,
            scenario_name=self.session.scenario_name,
            reasoning_effort=self.reasoning_effort,
        )
        await submission_phase.load_tools()
        submission_runner = submission_phase.get_agent()

        diag_text = state["diagnosis_report"][-1]
        frozen_context = submission_prompt_context(self.session_id)
        try:
            result = await submission_runner.ainvoke(
                {
                    "messages": [
                        HumanMessage(
                            content=(
                                "Use this frozen diagnosis report to make the one final "
                                f"canonical submission:\n{diag_text}\n\n{frozen_context}"
                            )
                        ),
                    ]
                },
                config={
                    "callbacks": [
                        AgentCallbackLogger(
                            phase=SUBMISSION, session_dir=self.session_dir
                        )
                    ],
                    "recursion_limit": self.max_steps,
                },
                debug=True,
            )
            return {
                "messages": result["messages"],
            }
        except GraphRecursionError:
            # The submit tool records submission.json the moment it is called,
            # so the submission may already be on disk; raising here would fail
            # the whole case and discard it. Either way the evaluator handles
            # the session better than a crash does (missing submission scores
            # as "no submission").
            submitted = (Path(self.session_dir) / "submission.json").exists()
            MessageLogger(phase=SUBMISSION, session_dir=self.session_dir).log(
                "error",
                {
                    "message": (
                        "Submission phase reached max recursion limit "
                        + (
                            "after a successful submission."
                            if submitted
                            else "without submitting."
                        )
                    )
                },
            )
            return {
                "messages": [
                    HumanMessage(
                        content="Error: submission phase did not finish within max steps."
                    )
                ],
            }
