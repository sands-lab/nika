"""LangGraph ReAct troubleshooting agent (``byo.langgraph``).

Diagnosis and submission each run a LangChain ``create_agent`` graph; the
shared :class:`~agent.base.NikaAgent` freezes the report and
advances the MCP gateway between them.
"""

import asyncio
import logging
import os
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from agent.base import NikaAgent, max_steps_report
from agent.byo.langgraph.middleware import (
    MAX_STEPS_EXCEEDED,
    message_text,
    react_recursion_limit,
)
from agent.byo.langgraph.phases.diagnosis import DiagnosisPhase
from agent.protocols import DIAGNOSIS
from agent.submission import submit_with_agent_model
from agent.utils.loggers import AgentCallbackLogger, MessageLogger
from nika.utils.logger import system_logger

load_dotenv()


logging.basicConfig(level=logging.INFO)


class BasicReActAgent(NikaAgent):
    def __init__(
        self,
        session_id: str,
        llm_provider: str = "openai",
        model: str = "gpt-5-mini",
        max_steps: int = 20,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
        stream_output: bool = True,
    ):
        super().__init__(
            session_id,
            model=model,
            llm_provider=llm_provider,
            max_steps=max_steps,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            stream_output=stream_output,
        )
        self.langfuse_handler = self._load_langfuse_handler()

        diagnosis_phase = DiagnosisPhase(
            session_id=session_id,
            llm_provider=llm_provider,
            model=model,
            scenario_name=self.scenario_name,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            max_steps=max_steps,
        )
        asyncio.run(diagnosis_phase.load_tools())
        self._diagnosis_runner = diagnosis_phase.get_agent()

    def _diagnosis_callbacks(self) -> tuple[AgentCallbackLogger, list[Any]]:
        cb = AgentCallbackLogger(phase=DIAGNOSIS, session_dir=self.trace_dir)
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
        cb, callbacks = self._diagnosis_callbacks()
        try:
            result = await self._diagnosis_runner.ainvoke(
                {"messages": [HumanMessage(content=task_description)]},
                config={
                    "callbacks": callbacks,
                    "recursion_limit": react_recursion_limit(self.max_steps),
                },
                # debug=True dumps every graph update (huge tool payloads) to
                # stdout and flashes over the benchmark Live dashboard.
                debug=False,
            )
        except ValidationError as e:
            return f"ERROR: diagnosis validation error: {e}"
        except MAX_STEPS_EXCEEDED:
            return max_steps_report(
                MessageLogger(phase=DIAGNOSIS, session_dir=self.trace_dir),
                max_steps=self.max_steps,
                latest_text=cb.last_text,
            )
        return message_text(result["messages"][-1])

    async def submit(self, diagnosis_report: str, context: dict) -> str:
        callbacks = [self.langfuse_handler] if self.langfuse_handler else []
        return await submit_with_agent_model(
            self, diagnosis_report, context, callbacks=callbacks
        )
