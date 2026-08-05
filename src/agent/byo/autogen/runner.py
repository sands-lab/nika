"""AutoGen phase runner with NIKA messages.jsonl logging."""

from __future__ import annotations

import os

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import ToolCallExecutionEvent, ToolCallRequestEvent
from autogen_core.models import ModelFamily
from autogen_ext.models.openai import OpenAIChatCompletionClient

from agent.utils.loggers import MessageLogger
from nika.service.mcp_server.registry import MCP_SERVER_PREFIXES

_KATHARA_PREFIXES = MCP_SERVER_PREFIXES

_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
_DEEPSEEK_MODEL_INFO = {
    "vision": False,
    "function_calling": True,
    "json_output": False,
    "family": ModelFamily.UNKNOWN,
    "structured_output": False,
}


def _short_tool_name(name: str) -> str:
    if name.startswith("task_mcp_server_"):
        return name.removeprefix("task_mcp_server_")
    for prefix in _KATHARA_PREFIXES:
        if name.startswith(prefix):
            return name.removeprefix(prefix)
    return name


def _uses_deepseek(model: str) -> bool:
    return model.lower().startswith("deepseek")


def create_model_client(model: str) -> OpenAIChatCompletionClient:
    if _uses_deepseek(model):
        api_key = os.environ.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY required for DeepSeek models: set it in .env before running byo.autogen."
            )
        return OpenAIChatCompletionClient(
            model=model,
            base_url=_DEEPSEEK_BASE_URL,
            api_key=api_key,
            model_info=_DEEPSEEK_MODEL_INFO,
        )
    return OpenAIChatCompletionClient(model=model)


async def run_logged_agent(
    *,
    agent: AssistantAgent,
    task: str,
    logger: MessageLogger,
    max_steps: int,
) -> tuple[str, bool]:
    """Run an AssistantAgent and log tool events to ``messages.jsonl``."""
    tool_rounds = 0
    final_text = ""

    async for event in agent.run_stream(task=task):
        if isinstance(event, TaskResult):
            if event.messages:
                last = event.messages[-1]
                content = getattr(last, "content", None)
                if isinstance(content, str) and content:
                    final_text = content
            continue

        if isinstance(event, ToolCallRequestEvent):
            tool_rounds += 1
            for call in event.content:
                logger.log(
                    "tool_start",
                    {
                        "tool": {"name": _short_tool_name(call.name)},
                        "input": call.arguments,
                    },
                )
        elif isinstance(event, ToolCallExecutionEvent):
            for result in event.content:
                if result.is_error:
                    logger.log("tool_error", {"error": str(result.content)})
                else:
                    logger.log(
                        "tool_end",
                        {
                            "output": str(result.content),
                            "output_type": "FunctionExecutionResult",
                        },
                    )

    return final_text, tool_rounds >= max_steps
