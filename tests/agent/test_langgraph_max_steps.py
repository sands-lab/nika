"""LangGraph max_steps counts LLM turns, not raw recursion nodes."""

from langchain.agents import create_agent
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolCall
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from agent.byo.langgraph.react_agent import _react_recursion_limit


class _AlwaysToolModel(BaseChatModel):
    n: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.n += 1
        msg = AIMessage(
            content="",
            tool_calls=[ToolCall(name="ping", args={}, id=f"c{self.n}")],
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


def test_react_recursion_limit_is_loose_backstop() -> None:
    assert _react_recursion_limit(20) > 20
    assert _react_recursion_limit(20) >= 40


def test_model_call_limit_enforces_exact_llm_turns() -> None:
    max_steps = 3
    model = _AlwaysToolModel()
    agent = create_agent(
        model=model,
        tools=[ping],
        middleware=[
            ModelCallLimitMiddleware(run_limit=max_steps, exit_behavior="error")
        ],
    )
    try:
        agent.invoke(
            {"messages": [HumanMessage("go")]},
            config={"recursion_limit": _react_recursion_limit(max_steps)},
        )
        raise AssertionError("expected ModelCallLimitExceededError")
    except ModelCallLimitExceededError:
        pass
    assert model.n == max_steps
