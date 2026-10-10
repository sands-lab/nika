"""LangGraph max_steps counts LLM turns, not raw recursion nodes."""

import pytest

from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolCall
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from agent.byo.langgraph.middleware import (
    MAX_TRUNCATION_RETRIES,
    TRUNCATION_NUDGE,
)
from agent.byo.langgraph.phases.diagnosis import DiagnosisPhase
from agent.byo.langgraph.phases.submission import SubmissionPhase
from agent.byo.langgraph.react_agent import _react_recursion_limit

pytestmark = pytest.mark.unit


class _AlwaysToolModel(BaseChatModel):
    responses: list[AIMessage] = []
    n: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.n += 1
        msg = AIMessage(
            content="",
            tool_calls=[ToolCall(name="ping", args={}, id=f"c{self.n}")],
        )
        if self.responses:
            msg = self.responses[min(self.n - 1, len(self.responses) - 1)].model_copy(
                update={"id": f"response-{self.n}"}
            )
        return ChatResult(generations=[ChatGeneration(message=msg)])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    @property
    def _llm_type(self) -> str:
        return "always-tool"

    def bind_tools(self, tools, **kwargs):
        return self


@tool
def ping() -> str:
    """ping"""
    return "pong"


@pytest.mark.parametrize("phase_cls", [DiagnosisPhase, SubmissionPhase])
@pytest.mark.parametrize(
    ("responses", "max_steps", "expected_calls", "limited"),
    [
        (
            [
                AIMessage(content="", response_metadata={"finish_reason": "length"}),
                AIMessage(content="final report"),
            ],
            10,
            2,
            False,
        ),
        (
            [AIMessage(content="", response_metadata={"finish_reason": "length"})],
            10,
            MAX_TRUNCATION_RETRIES + 1,
            False,
        ),
        (
            [AIMessage(content="", response_metadata={"finish_reason": "length"})],
            2,
            2,
            True,
        ),
        ([], 20, 20, True),
        ([AIMessage(content="final report")], 10, 1, False),
        (
            [
                AIMessage(
                    content="",
                    response_metadata={"finish_reason": "length"},
                    tool_calls=[ToolCall(name="ping", args={}, id="truncated")],
                ),
                AIMessage(content="final report"),
            ],
            10,
            2,
            False,
        ),
    ],
    ids=["recover", "retry-cap", "retry-budget", "tool-budget", "normal", "tool-call"],
)
async def test_phase_turns_respect_truncation_and_budget(
    phase_cls, responses, max_steps, expected_calls, limited
) -> None:
    model = _AlwaysToolModel(responses=responses)
    phase = phase_cls.__new__(phase_cls)
    phase.llm, phase.tools, phase.max_steps = model, [ping], max_steps
    agent = phase.get_agent()
    if limited:
        with pytest.raises(ModelCallLimitExceededError):
            await agent.ainvoke(
                {"messages": [HumanMessage("go")]},
                config={"recursion_limit": _react_recursion_limit(max_steps)},
            )
    else:
        result = await agent.ainvoke(
            {"messages": [HumanMessage("go")]},
            config={"recursion_limit": _react_recursion_limit(max_steps)},
        )
        messages = result["messages"]
        assert (
            messages[-1].content
            == responses[min(expected_calls - 1, len(responses) - 1)].content
        )
        nudges = [
            m
            for m in messages
            if isinstance(m, HumanMessage) and m.content == TRUNCATION_NUDGE
        ]
        assert len(nudges) == (
            expected_calls - 1
            if responses[0].response_metadata.get("finish_reason") == "length"
            and not responses[0].tool_calls
            else 0
        )
    assert model.n == expected_calls
