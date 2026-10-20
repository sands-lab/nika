from collections.abc import Sequence
from typing import Any

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import HumanMessage
from langchain_core.tools.structured import StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from agent.base import max_steps_report
from agent.byo.langgraph.middleware import (
    MAX_STEPS_EXCEEDED,
    TruncationRetryMiddleware,
    message_text,
    react_recursion_limit,
)
from agent.utils.loggers import AgentCallbackLogger, MessageLogger
from agent.utils.submission_context import submission_user_prompt
from agent.utils.template import SUBMIT_PROMPT_TEMPLATE
from agent.llm.model_factory import load_model
from agent.utils.mcp_client import load_session_mcp_config
from agent.utils.mcp_servers import harden_mcp_tools
from agent.protocols import SUBMISSION

load_dotenv()


class SubmissionPhase:
    """LangChain ReAct worker for the submission phase."""

    def __init__(
        self,
        session_id: str,
        llm_provider: str = "openai",
        model: str = "gpt-5-mini",
        scenario_name: str = "",
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
        max_steps: int = 20,
    ):
        mcp_server_config = load_session_mcp_config(
            session_id, scenario_name, phase=SUBMISSION
        )
        self.client = MultiServerMCPClient(connections=mcp_server_config)
        self.tools = None
        self.max_steps = max_steps

        self.llm = load_model(
            llm_provider=llm_provider,
            model=model,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
        )

    async def load_tools(self):
        self.tools: list[StructuredTool] = await self.client.get_tools()
        harden_mcp_tools(self.tools)
        for tool in self.tools:
            tool.handle_tool_error = True
            tool.handle_validation_error = True

    def get_agent(self):
        agent = create_agent(
            model=self.llm,
            system_prompt=SUBMIT_PROMPT_TEMPLATE,
            tools=self.tools,
            name=SUBMISSION,
            # after_model hooks run in reverse order: list the retry first so
            # the call limit counts a truncated turn before it is re-prompted.
            middleware=[
                TruncationRetryMiddleware(),
                ModelCallLimitMiddleware(
                    run_limit=self.max_steps,
                    exit_behavior="error",
                ),
            ],
        )
        return agent

    async def run(
        self,
        diagnosis_report: str,
        context: dict,
        *,
        trace_dir: str,
        callbacks: Sequence[Any] = (),
    ) -> str:
        """Answer from the frozen report with the task MCP ``submit`` tool."""
        await self.load_tools()
        cb = AgentCallbackLogger(phase=SUBMISSION, session_dir=trace_dir)
        try:
            result = await self.get_agent().ainvoke(
                {
                    "messages": [
                        HumanMessage(
                            content=submission_user_prompt(diagnosis_report, context)
                        )
                    ]
                },
                config={
                    "callbacks": [cb, *callbacks],
                    "recursion_limit": react_recursion_limit(self.max_steps),
                },
                debug=False,
            )
        except MAX_STEPS_EXCEEDED:
            # submit() records submission.json when called, so an answer may
            # already be on disk; the evaluator scores a missing one as "no
            # submission", which beats failing the whole case.
            return max_steps_report(
                MessageLogger(phase=SUBMISSION, session_dir=trace_dir),
                max_steps=self.max_steps,
                latest_text=cb.last_text,
            )
        return message_text(result["messages"][-1])
