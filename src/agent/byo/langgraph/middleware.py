"""LangChain agent middleware and limits shared by the LangGraph phases.

The default structured submission (:mod:`agent.submission`) runs the same
submission graph for agents that implement only ``diagnose``.
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphRecursionError

from agent.utils.loggers import message_text as message_text

# Binding LLM-turn limit is ModelCallLimitMiddleware(run_limit=max_steps).
# create_agent may count before_model/model/after_model/tools as separate
# recursion steps, so keep recursion_limit as a loose backstop only.
_RECURSION_STEPS_PER_LLM_TURN = 5
_RECURSION_LIMIT_SLACK = 10

MAX_STEPS_EXCEEDED = (GraphRecursionError, ModelCallLimitExceededError)


def react_recursion_limit(max_steps: int) -> int:
    return max_steps * _RECURSION_STEPS_PER_LLM_TURN + _RECURSION_LIMIT_SLACK


# Consecutive unfinished turns to re-prompt before letting the phase end.
MAX_TRUNCATION_RETRIES = 2

TRUNCATION_NUDGE = (
    "Your previous response ended (or was cut off at the output token limit) "
    "before it produced a tool call or a final answer. Reason more briefly, "
    "then either call the next tool or give your final answer."
)


def _is_unfinished(message: Any) -> bool:
    """True for a turn with no tool call that hit ``max_tokens`` or has no text.

    Qwen-style reasoning models sometimes stop (``finish_reason=stop``) while
    still inside their reasoning, leaving no answer text at all.
    """
    if not isinstance(message, AIMessage) or message.tool_calls:
        return False
    if message.response_metadata.get("finish_reason") == "length":
        return True
    return not message.text.strip()


class TruncationRetryMiddleware(AgentMiddleware):
    """Re-prompt the model when a turn ends with no tool call and no answer.

    ``create_agent`` treats any turn without tool calls as the final answer, so
    a reasoning model that spends its whole output budget thinking, or stops
    mid-reasoning, would end the phase with an empty answer. List it before
    ``ModelCallLimitMiddleware`` so the limit counts each unfinished turn and
    retries use ``max_steps``.
    """

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:  # noqa: ARG002
        messages = state["messages"]
        if not messages or not _is_unfinished(messages[-1]):
            return None
        retries = 0
        for prev, message in zip(messages[-3::-2], messages[-2::-2]):
            if not (
                isinstance(message, HumanMessage)
                and message.content == TRUNCATION_NUDGE
                and _is_unfinished(prev)
            ):
                break
            retries += 1
        if retries >= MAX_TRUNCATION_RETRIES:
            return None
        return {
            "messages": [HumanMessage(content=TRUNCATION_NUDGE)],
            "jump_to": "model",
        }
