"""AutoGen phase runner with NIKA messages.jsonl logging."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import TaskResult
from autogen_agentchat.messages import (
    TextMessage,
    ToolCallExecutionEvent,
    ToolCallRequestEvent,
)
from autogen_core.models import ChatCompletionClient, ModelFamily
from autogen_ext.models.anthropic import AnthropicChatCompletionClient
from autogen_ext.models.openai import OpenAIChatCompletionClient
from autogen_ext.tools.mcp import create_mcp_server_session, mcp_server_tools

from agent.byo.autogen.config import to_mcp_params

from agent.utils.loggers import (
    MessageLogger,
    PendingToolCallTracker,
    tool_event_payload,
)
from agent.utils.usage import normalize_usage
from nika.mcp.registry import MCP_SERVER_PREFIXES
from agent.utils.provider_env import (
    require_provider,
    DEEPSEEK_OPENAI_BASE_URL,
    ENV_ANTHROPIC_API_KEY,
    ENV_ANTHROPIC_BASE_URL,
    ENV_DEEPSEEK_API_KEY,
    ENV_OPENAI_API_KEY,
    ENV_OPENAI_BASE_URL,
    resolve_custom_api_key,
    resolve_custom_base_url,
)
from agent.utils.reasoning_capture import reasoning_fields_for_log
from agent.utils.reasoning_effort import map_anthropic_effort
from agent.utils.two_phase import max_steps_report


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
    prov = require_provider(provider)

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
    payload = {
        "text": content if isinstance(content, str) else "",
        "usage_metadata": normalize_usage(usage),
    }
    # ThoughtEvent / messages may carry reasoning on the event or nested message.
    payload.update(reasoning_fields_for_log(event))
    if not payload.get("reasoning_content"):
        nested = getattr(event, "chat_message", None) or getattr(event, "message", None)
        payload.update(reasoning_fields_for_log(nested))
    logger.log("llm_end", payload)


async def _run_logged_agent(
    *,
    agent: AssistantAgent,
    task: str,
    logger: MessageLogger,
) -> tuple[str, int]:
    """Run *agent*, log events to ``messages.jsonl``; return ``(text, tool_rounds)``."""
    tool_rounds = 0
    final_text = ""
    pending_tool_calls = PendingToolCallTracker()

    async for event in agent.run_stream(task=task):
        if isinstance(event, TaskResult):
            last = event.messages[-1] if event.messages else None
            # A ToolCallSummaryMessage is tool output, not an assistant report.
            if isinstance(last, TextMessage) and last.content:
                final_text = last.content
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

    return final_text, tool_rounds


@asynccontextmanager
async def open_mcp_tools(server_configs: dict) -> AsyncIterator[list]:
    """Open one MCP session per server and yield their AutoGen tools."""
    sessions: list = []
    tools: list = []
    try:
        for cfg in server_configs.values():
            params = to_mcp_params(cfg)
            session_cm = create_mcp_server_session(params)
            session = await session_cm.__aenter__()
            await session.initialize()
            sessions.append(session_cm)
            tools.extend(await mcp_server_tools(params, session=session))
        yield tools
    finally:
        for session_cm in reversed(sessions):
            await session_cm.__aexit__(None, None, None)


async def run_autogen_phase(
    *,
    name: str,
    system_message: str,
    task: str,
    server_configs: dict,
    model: str,
    provider: str,
    reasoning_effort: str | None,
    max_steps: int,
    logger: MessageLogger,
) -> str:
    """Run one phase with at most *max_steps* model calls.

    AutoGen makes one model call per tool iteration plus one reflection call
    when the last iteration still requested tools, so ``max_tool_iterations``
    is ``max_steps - 1`` and the reflection is the final turn. When that limit
    is hit, the reflection text is the phase report.
    """
    model_client = create_model_client(
        model, provider=provider, reasoning_effort=reasoning_effort
    )
    max_tool_iterations = max(1, max_steps - 1)
    async with open_mcp_tools(server_configs) as tools:
        agent = AssistantAgent(
            name=name,
            model_client=model_client,
            tools=tools,
            system_message=system_message,
            reflect_on_tool_use=max_steps > 1,
            max_tool_iterations=max_tool_iterations,
        )
        try:
            text, tool_rounds = await _run_logged_agent(
                agent=agent, task=task, logger=logger
            )
        finally:
            await model_client.close()
    if tool_rounds >= max_tool_iterations:
        return max_steps_report(logger, max_steps=max_steps, latest_text=text)
    return text
