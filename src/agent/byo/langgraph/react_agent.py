"""LangGraph ReAct troubleshooting agent (``byo.langgraph``).

Diagnosis and submission each run a LangChain ``create_agent`` graph; the
shared :class:`~agent.utils.two_phase.TwoPhaseAgent` freezes the report and
advances the MCP gateway between them.
"""

import asyncio
import logging
import os
from typing import Any

from dotenv import load_dotenv
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.messages import HumanMessage
from langgraph.errors import GraphRecursionError
from pydantic import ValidationError

from agent.byo.langgraph.phases.diagnosis import DiagnosisPhase
from agent.byo.langgraph.phases.submission import SubmissionPhase
from agent.protocols import DIAGNOSIS, SUBMISSION
from agent.utils.loggers import AgentCallbackLogger, MessageLogger
from agent.utils.submission_context import submission_user_prompt
from agent.utils.two_phase import TwoPhaseAgent, max_steps_report
from nika.utils.logger import system_logger
from nika.utils.session import Session

load_dotenv()


logging.basicConfig(level=logging.INFO)

# Binding LLM-turn limit is ModelCallLimitMiddleware(run_limit=max_steps).
# create_agent may count before_model/model/after_model/tools as separate
# recursion steps, so keep recursion_limit as a loose backstop only.
_RECURSION_STEPS_PER_LLM_TURN = 4
_RECURSION_LIMIT_SLACK = 10


def _react_recursion_limit(max_steps: int) -> int:
    return max_steps * _RECURSION_STEPS_PER_LLM_TURN + _RECURSION_LIMIT_SLACK


_MAX_STEPS_EXCEEDED = (GraphRecursionError, ModelCallLimitExceededError)


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
            if not isinstance(block, dict) or block.get("type") == "text"
        )
    return str(content)


class BasicReActAgent(TwoPhaseAgent):
    def __init__(
        self,
        session_id: str,
        llm_provider: str = "openai",
        model: str = "gpt-5-mini",
        max_steps: int = 20,
        reasoning_effort: str | None = None,
        stream_output: bool = False,
    ):
        self.session_id = session_id
        self.max_steps = max_steps
        self.llm_provider = llm_provider
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.stream_output = stream_output
        self.session = Session()
        self.session.load_running_session(session_id=session_id)
        self.session_dir = self.session.session_dir
        self.trace_dir = self.session_dir

        self.langfuse_handler = self._load_langfuse_handler()

        diagnosis_phase = DiagnosisPhase(
            session_id=session_id,
            llm_provider=llm_provider,
            model=model,
            scenario_name=self.session.scenario_name,
            reasoning_effort=reasoning_effort,
            max_steps=max_steps,
        )
        asyncio.run(diagnosis_phase.load_tools())
        self._diagnosis_runner = diagnosis_phase.get_agent()

    def _callbacks(self, phase: str) -> tuple[AgentCallbackLogger, list[Any]]:
        cb = AgentCallbackLogger(phase=phase, session_dir=self.session_dir)
        callbacks: list[Any] = [cb]
        if self.langfuse_handler is not None:
            callbacks.append(self.langfuse_handler)
        return cb, callbacks

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

    async def diagnose(self, task_description: str) -> str:
        cb, callbacks = self._callbacks(DIAGNOSIS)
        try:
            result = await self._diagnosis_runner.ainvoke(
                {"messages": [HumanMessage(content=task_description)]},
                config={
                    "callbacks": callbacks,
                    "recursion_limit": _react_recursion_limit(self.max_steps),
                },
                # debug=True dumps every graph update (huge tool payloads) to
                # stdout and flashes over the benchmark Live dashboard.
                debug=False,
            )
        except ValidationError as e:
            return f"ERROR: diagnosis validation error: {e}"
        except _MAX_STEPS_EXCEEDED:
            return max_steps_report(
                MessageLogger(phase=DIAGNOSIS, session_dir=self.session_dir),
                max_steps=self.max_steps,
                latest_text=cb.last_text,
            )
        return _message_text(result["messages"][-1])

    async def submit(self, diagnosis_report: str, context: dict) -> str:
        submission_phase = SubmissionPhase(
            session_id=self.session_id,
            llm_provider=self.llm_provider,
            model=self.model,
            scenario_name=self.session.scenario_name,
            reasoning_effort=self.reasoning_effort,
            max_steps=self.max_steps,
        )
        await submission_phase.load_tools()
        submission_runner = submission_phase.get_agent()
        cb, callbacks = self._callbacks(SUBMISSION)
        try:
            result = await submission_runner.ainvoke(
                {
                    "messages": [
                        HumanMessage(
                            content=submission_user_prompt(diagnosis_report, context)
                        )
                    ]
                },
                config={
                    "callbacks": callbacks,
                    "recursion_limit": _react_recursion_limit(self.max_steps),
                },
                debug=False,
            )
        except _MAX_STEPS_EXCEEDED:
            # submit() records submission.json when called, so an answer may
            # already be on disk; the evaluator scores a missing one as "no
            # submission", which beats failing the whole case.
            return max_steps_report(
                MessageLogger(phase=SUBMISSION, session_dir=self.session_dir),
                max_steps=self.max_steps,
                latest_text=cb.last_text,
            )
        return _message_text(result["messages"][-1])
