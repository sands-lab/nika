"""LangGraph max_steps counts LLM turns, not raw recursion nodes."""

import pytest

from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolCall, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from agent.byo.langgraph.middleware import (
    MAX_TRUNCATION_RETRIES,
    TRUNCATION_NUDGE,
    react_recursion_limit,
)
from agent.byo.langgraph.phases.diagnosis import DiagnosisPhase
from agent.byo.langgraph.phases.submission import SubmissionPhase

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
        (
            [
                AIMessage(content="", response_metadata={"finish_reason": "stop"}),
                AIMessage(content="final report"),
            ],
            10,
            2,
            False,
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
    ids=[
        "recover",
        "retry-cap",
        "retry-budget",
        "empty-stop",
        "tool-budget",
        "normal",
        "tool-call",
    ],
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
                config={"recursion_limit": react_recursion_limit(max_steps)},
            )
    else:
        result = await agent.ainvoke(
            {"messages": [HumanMessage("go")]},
            config={"recursion_limit": react_recursion_limit(max_steps)},
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
            if not responses[0].tool_calls
            and (
                responses[0].response_metadata.get("finish_reason") == "length"
                or not responses[0].content
            )
            else 0
        )
    assert model.n == expected_calls


@pytest.mark.parametrize("phase_cls", [DiagnosisPhase, SubmissionPhase])
@pytest.mark.parametrize(
    ("recover", "max_steps"), [(True, 10), (False, 10), (False, 2)]
)
async def test_malformed_tool_calls_receive_feedback_within_existing_limits(
    phase_cls, recover, max_steps
):
    malformed = AIMessage(
        content="I will inspect the network.",
        invalid_tool_calls=[
            {"name": "ping", "args": "{}}", "id": f"bad-{i}", "error": "invalid JSON"}
            for i in range(2)
        ],
    )
    responses = [malformed]
    if recover:
        responses += [
            AIMessage(
                content="", tool_calls=[ToolCall(name="ping", args={}, id="good")]
            ),
            AIMessage(content="final report"),
        ]
    model = _AlwaysToolModel(responses=responses)
    phase = phase_cls.__new__(phase_cls)
    phase.llm, phase.tools, phase.max_steps = model, [ping], max_steps
    if max_steps == 2:
        with pytest.raises(ModelCallLimitExceededError):
            await phase.get_agent().ainvoke({"messages": [HumanMessage("go")]})
        assert model.n == 2
        return
    result = await phase.get_agent().ainvoke({"messages": [HumanMessage("go")]})
    messages = result["messages"]
    errors = [m for m in messages if isinstance(m, ToolMessage) and m.status == "error"]
    assert {m.tool_call_id for m in errors} == {"bad-0", "bad-1"}
    assert all("JSON" in m.content for m in errors)
    assert len(errors) == (2 if recover else 2 * MAX_TRUNCATION_RETRIES)
    if recover:
        assert model.n == 3
        assert any(isinstance(m, ToolMessage) and m.content == "pong" for m in messages)
        assert messages[-1].content == "final report"
    else:
        assert model.n == MAX_TRUNCATION_RETRIES + 1


@pytest.mark.parametrize("rejected_first", [False, True])
async def test_submission_stops_after_acceptance_and_retries_rejection(rejected_first):
    calls = []

    @tool
    def submit() -> str:
        """Submit the final diagnosis."""
        calls.append(1)
        if rejected_first and len(calls) == 1:
            return "Submission rejected: correct the arguments."
        return "Submission success."

    responses = [
        AIMessage(content="", tool_calls=[ToolCall(name="submit", args={}, id=str(i))])
        for i in range(3)
    ]
    model = _AlwaysToolModel(responses=responses)
    phase = SubmissionPhase.__new__(SubmissionPhase)
    phase.llm, phase.tools, phase.max_steps = (
        model,
        [submit],
        2 if rejected_first else 1,
    )
    result = await phase.get_agent().ainvoke(
        {"messages": [HumanMessage("submit the report")]}
    )
    assert model.n == len(calls) == (2 if rejected_first else 1)
    assert isinstance(result["messages"][-1], ToolMessage)
    assert result["messages"][-1].content == "Submission success."
