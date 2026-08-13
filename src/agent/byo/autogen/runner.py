"""AutoGen phase runner with NIKA messages.jsonl logging."""

from __future__ import annotations

import os

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import ToolCallExecutionEvent, ToolCallRequestEvent
from autogen_core.models import ModelFamily
from autogen_ext.models.openai import OpenAIChatCompletionClient

from agent.utils.loggers import MessageLogger
from agent.utils.usage import normalize_usage
from nika.service.mcp_server.registry import MCP_SERVER_PREFIXES
from agent.utils.provider_env import (
    DEEPSEEK_OPENAI_BASE_URL,
    ENV_DEEPSEEK_API_KEY,
    ENV_OPENAI_API_KEY,
    ENV_OPENAI_BASE_URL,
    resolve_custom_api_key,
    resolve_custom_base_url,
)

_KATHARA_PREFIXES = MCP_SERVER_PREFIXES

_DEEPSEEK_MODEL_INFO = {
    "vision": False,
    "function_calling": True,
    "json_output": False,
    "family": ModelFamily.UNKNOWN,
    "structured_output": False,
}

_OPENAI_COMPAT_MODEL_INFO = {
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


def _resolve_provider(provider: str | None = None) -> str:
    if provider and provider.strip():
        return provider.strip().lower()
    env = (os.environ.get("NIKA_LLM_PROVIDER") or "").strip().lower()
    if env:
        return env
    # Legacy heuristic: deepseek-* model ids
    return ""


def create_model_client(
    model: str, *, provider: str | None = None
) -> OpenAIChatCompletionClient:
    """Build an OpenAI-compatible AutoGen client for the active provider."""
    prov = _resolve_provider(provider)
    if not prov and model.lower().startswith("deepseek"):
        prov = "deepseek"

    if prov == "deepseek":
        api_key = os.environ.get(ENV_DEEPSEEK_API_KEY) or os.environ.get(
            ENV_OPENAI_API_KEY
        )
        if not api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY required for DeepSeek models: set it in .env "
                "and NIKA_LLM_PROVIDER=deepseek before running byo.autogen."
            )
        return OpenAIChatCompletionClient(
            model=model,
            base_url=os.environ.get(ENV_OPENAI_BASE_URL) or DEEPSEEK_OPENAI_BASE_URL,
            api_key=api_key,
            model_info=_DEEPSEEK_MODEL_INFO,
        )

    if prov == "custom":
        base_url = resolve_custom_base_url() or os.environ.get(ENV_OPENAI_BASE_URL, "")
        if not base_url:
            raise ValueError(
                "NIKA_CUSTOM_BASE_URL required for custom provider "
                "(or set OPENAI_BASE_URL for legacy OpenAI-compatible endpoints)."
            )
        api_key = (
            resolve_custom_api_key() or os.environ.get(ENV_OPENAI_API_KEY) or "no-key"
        )
        return OpenAIChatCompletionClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            model_info=_OPENAI_COMPAT_MODEL_INFO,
        )

    # openai (default): rely on OPENAI_API_KEY / OPENAI_BASE_URL from env
    kwargs: dict = {"model": model}
    base = os.environ.get(ENV_OPENAI_BASE_URL, "").strip()
    key = os.environ.get(ENV_OPENAI_API_KEY, "").strip()
    if base:
        kwargs["base_url"] = base
        kwargs["model_info"] = _OPENAI_COMPAT_MODEL_INFO
    if key:
        kwargs["api_key"] = key
    return OpenAIChatCompletionClient(**kwargs)


def _log_event_usage(logger: MessageLogger, event: object) -> None:
    usage = getattr(event, "models_usage", None)
    if usage is None:
        return
    content = getattr(event, "content", None)
    logger.log(
        "llm_end",
        {
            "text": content if isinstance(content, str) else "",
            "usage_metadata": normalize_usage(usage),
        },
    )


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

        _log_event_usage(logger, event)
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
