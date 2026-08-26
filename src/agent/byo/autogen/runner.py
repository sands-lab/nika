"""AutoGen phase runner with NIKA messages.jsonl logging."""

from __future__ import annotations

import os

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import ToolCallExecutionEvent, ToolCallRequestEvent
from autogen_core.models import ChatCompletionClient, ModelFamily
from autogen_ext.models.anthropic import AnthropicChatCompletionClient
from autogen_ext.models.openai import OpenAIChatCompletionClient

from agent.utils.loggers import (
    MessageLogger,
    PendingToolCallTracker,
    tool_event_payload,
)
from agent.utils.usage import normalize_usage
from nika.service.mcp_server.registry import MCP_SERVER_PREFIXES
from agent.utils.provider_env import (
    DEEPSEEK_OPENAI_BASE_URL,
    ENV_ANTHROPIC_API_KEY,
    ENV_ANTHROPIC_BASE_URL,
    ENV_DEEPSEEK_API_KEY,
    ENV_OPENAI_API_KEY,
    ENV_OPENAI_BASE_URL,
    resolve_custom_api_key,
    resolve_custom_base_url,
)
from agent.utils.reasoning_effort import map_anthropic_effort


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

# Explicit info so Anthropic-compatible / non-catalog models keep tool calling.
_ANTHROPIC_COMPAT_MODEL_INFO = {
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


def _resolve_provider(provider: str) -> str:
    if not provider or not str(provider).strip():
        raise ValueError(
            "Missing LLM provider: set agent.provider in config/nika.yaml "
            "or pass -p/--provider."
        )
    return str(provider).strip().lower()


def _inject_anthropic_output_config(
    client: AnthropicChatCompletionClient, reasoning_effort: str | None
) -> AnthropicChatCompletionClient:
    """Wrap the underlying Anthropic SDK so create/stream send output_config.effort.

    Autogen's AnthropicChatCompletionClient does not forward ``output_config``;
    inject it on the low-level Messages API instead.
    """
    effort = map_anthropic_effort(reasoning_effort)
    if effort is None:
        return client
    messages_api = client._client.messages
    orig_create = messages_api.create
    orig_stream = messages_api.stream

    def _with_effort(kwargs: dict) -> dict:
        out = dict(kwargs)
        out.setdefault("output_config", {"effort": effort})
        return out

    def create(*args, **kwargs):
        return orig_create(*args, **_with_effort(kwargs))

    def stream(*args, **kwargs):
        return orig_stream(*args, **_with_effort(kwargs))

    messages_api.create = create  # type: ignore[method-assign]
    messages_api.stream = stream  # type: ignore[method-assign]
    return client


def create_model_client(
    model: str,
    *,
    provider: str,
    reasoning_effort: str | None = None,
) -> ChatCompletionClient:
    """Build an AutoGen chat client for the active provider."""
    prov = _resolve_provider(provider)

    if prov == "anthropic":
        api_key = os.environ.get(ENV_ANTHROPIC_API_KEY, "").strip()
        if not api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY required for Anthropic models: set it in .env "
                "and set agent.provider to anthropic in config/nika.yaml. "
                "For DeepSeek Anthropic-compat (Claude agents), use "
                "agent.provider: deepseek. For other Anthropic-compatible "
                "gateways, set agent.custom.base_url (same field as custom)."
            )
        kwargs: dict = {
            "model": model,
            "api_key": api_key,
            "model_info": _ANTHROPIC_COMPAT_MODEL_INFO,
        }
        base = (
            os.environ.get(ENV_ANTHROPIC_BASE_URL, "").strip()
            or resolve_custom_base_url()
        )
        if base:
            kwargs["base_url"] = base
        client = AnthropicChatCompletionClient(**kwargs)
        return _inject_anthropic_output_config(client, reasoning_effort)

    if prov == "deepseek":
        api_key = os.environ.get(ENV_DEEPSEEK_API_KEY) or os.environ.get(
            ENV_OPENAI_API_KEY
        )
        if not api_key:
            raise ValueError(
                "DEEPSEEK_API_KEY required for DeepSeek models: set it in .env "
                "and set agent.provider to deepseek in config/nika.yaml."
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
            try:
                from nika.run_config.loader import get_run_config

                base_url = (get_run_config().agent.custom.base_url or "").strip()
            except Exception:  # noqa: BLE001
                base_url = ""
        if not base_url:
            raise ValueError(
                "agent.custom.base_url required for custom provider "
                "in config/nika.yaml."
            )
        api_key = (
            resolve_custom_api_key() or os.environ.get(ENV_OPENAI_API_KEY) or "no-key"
        )
        kwargs = {
            "model": model,
            "base_url": base_url,
            "api_key": api_key,
            "model_info": _OPENAI_COMPAT_MODEL_INFO,
        }
        if reasoning_effort is not None:
            kwargs["reasoning_effort"] = reasoning_effort
        return OpenAIChatCompletionClient(**kwargs)

    # openai: rely on OPENAI_API_KEY / OPENAI_BASE_URL from env
    kwargs = {"model": model}
    base = os.environ.get(ENV_OPENAI_BASE_URL, "").strip()
    key = os.environ.get(ENV_OPENAI_API_KEY, "").strip()
    if base:
        kwargs["base_url"] = base
        kwargs["model_info"] = _OPENAI_COMPAT_MODEL_INFO
    if key:
        kwargs["api_key"] = key
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
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
    pending_tool_calls = PendingToolCallTracker()

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
                    pending_tool_calls.register(
                        name=_short_tool_name(call.name),
                        input=call.arguments,
                        tool_call_id=call.id,
                    ),
                )
        elif isinstance(event, ToolCallExecutionEvent):
            for result in event.content:
                tool_name = _short_tool_name(result.name)
                resolved = pending_tool_calls.resolve(
                    name=tool_name,
                    tool_call_id=result.call_id,
                )
                correlation = tool_event_payload(
                    name=tool_name or resolved.get("name") or None,
                    input=resolved.get("input"),
                    tool_call_id=result.call_id,
                )
                if result.is_error:
                    logger.log(
                        "tool_error",
                        {**correlation, "error": str(result.content)},
                    )
                else:
                    logger.log(
                        "tool_end",
                        {
                            **correlation,
                            "output": str(result.content),
                            "output_type": "FunctionExecutionResult",
                        },
                    )

    return final_text, tool_rounds >= max_steps
