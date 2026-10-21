"""LangGraph ReAct troubleshooting agent (``byo.langgraph``).

Diagnosis and submission each run a LangChain ``create_agent`` graph; the
host freezes the report and advances the MCP gateway between them.
"""

import logging

from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from agent.base import DiagnosisAgent, max_steps_report
from agent.byo.langgraph.middleware import (
    MAX_STEPS_EXCEEDED,
    message_text,
    react_recursion_limit,
)
from agent.byo.langgraph.phases.diagnosis import DiagnosisPhase
from agent.protocols import DIAGNOSIS
from agent.utils.loggers import AgentCallbackLogger, MessageLogger

logger = logging.getLogger(__name__)


class BasicReActAgent(DiagnosisAgent):
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

    async def diagnose(self, task_description: str) -> str:
        diagnosis_phase = DiagnosisPhase(
            llm_provider=self.llm_provider,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            max_tokens=self.max_tokens,
            max_steps=self.max_steps,
        )
        await diagnosis_phase.load_tools()
        cb = AgentCallbackLogger(phase=DIAGNOSIS, session_dir=self.trace_dir)
        try:
            result = await diagnosis_phase.get_agent().ainvoke(
                {"messages": [HumanMessage(content=task_description)]},
                config={
                    "callbacks": [cb],
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
