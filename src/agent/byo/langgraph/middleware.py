"""LangChain agent middleware shared by the LangGraph diagnosis and submission phases."""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, HumanMessage

# Consecutive truncated turns to re-prompt before letting the phase end.
MAX_TRUNCATION_RETRIES = 2

TRUNCATION_NUDGE = (
    "Your previous response was cut off at the output token limit before it "
    "finished, so it produced no tool call and no final answer. Reason more "
    "briefly, then either call the next tool or give your final answer."
)


def _is_truncated(message: Any) -> bool:
    """True for a model turn cut off by ``max_tokens`` without a tool call."""
    if not isinstance(message, AIMessage) or message.tool_calls:
        return False
    return message.response_metadata.get("finish_reason") == "length"


class TruncationRetryMiddleware(AgentMiddleware):
    """Re-prompt the model when a turn hits ``max_tokens`` with no tool call.

    ``create_agent`` treats any turn without tool calls as the final answer, so
    a reasoning model that spends its whole output budget thinking would end
    the phase with an empty answer. List it before ``ModelCallLimitMiddleware``
    so the limit counts each truncated turn and retries use ``max_steps``.
    """

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:  # noqa: ARG002
        messages = state["messages"]
        if not messages or not _is_truncated(messages[-1]):
            return None
        retries = 0
        for prev, message in zip(messages[-3::-2], messages[-2::-2]):
            if not (
                isinstance(message, HumanMessage)
                and message.content == TRUNCATION_NUDGE
                and _is_truncated(prev)
            ):
                break
            retries += 1
        if retries >= MAX_TRUNCATION_RETRIES:
            return None
        return {
            "messages": [HumanMessage(content=TRUNCATION_NUDGE)],
            "jump_to": "model",
        }
