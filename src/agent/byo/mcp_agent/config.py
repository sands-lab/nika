"""Bridge the session MCP server config to mcp-agent Settings."""

from __future__ import annotations

import os
import warnings

import pydantic

# mcp-agent 0.2.6 still defines an after-model classmethod validator on
# OpenTelemetrySettings; suppress the import-time Pydantic 2.12 deprecation.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", pydantic.PydanticDeprecatedSince212)
    from mcp_agent.config import (
        AnthropicSettings,
        MCPServerSettings,
        MCPSettings,
        OpenAISettings,
        Settings,
    )

from agent.llm.endpoints import (
    ENV_ANTHROPIC_API_KEY,
    ENV_ANTHROPIC_BASE_URL,
    ENV_OPENAI_API_KEY,
    ENV_OPENAI_BASE_URL,
    UNAUTHENTICATED_API_KEY,
    openai_api_key,
    openai_base_url,
    require_provider,
)
from agent.utils.mcp_client import mcp_read_timeout_seconds, session_mcp_config
from agent.utils.reasoning_effort import anthropic_output_config


def _to_server_settings(server: dict) -> MCPServerSettings:
    transport = server.get("transport", "stdio")
    read_timeout = mcp_read_timeout_seconds()
    # mcp-agent expects int | None; keep None when disabled (0 / unset path).
    read_timeout_int = int(read_timeout) if read_timeout is not None else None
    if transport == "http":
        return MCPServerSettings(
            transport="streamable_http",
            url=server["url"],
            headers=dict(server.get("headers") or {}),
            read_timeout_seconds=read_timeout_int,
            http_timeout_seconds=read_timeout_int,
        )
    return MCPServerSettings(
        transport=server.get("transport", "stdio"),
        command=server["command"],
        args=server.get("args", []),
        env=server.get("env"),
        read_timeout_seconds=read_timeout_int,
    )


# mcp-agent OpenAISettings / RequestParams accept these only (not minimal/xhigh).
_MCP_REASONING_EFFORT_LEVELS = ("none", "low", "medium", "high")


def _mcp_reasoning_effort(reasoning_effort: str | None) -> str | None:
    if reasoning_effort is None:
        return None
    if reasoning_effort not in _MCP_REASONING_EFFORT_LEVELS:
        raise ValueError(
            "byo.mcp_agent reasoning_effort must be one of "
            f"{', '.join(_MCP_REASONING_EFFORT_LEVELS)}, got {reasoning_effort!r}"
        )
    return reasoning_effort


def _openai_settings_for_provider(
    model: str,
    provider: str,
    *,
    reasoning_effort: str | None = None,
) -> OpenAISettings:
    prov = require_provider(provider)
    effort = _mcp_reasoning_effort(reasoning_effort)
    # DeepSeek OpenAI-compat does not take reasoning_effort.
    apply_effort = effort is not None and prov != "deepseek"
    if prov == "deepseek":
        return OpenAISettings(
            default_model=model,
            api_key=openai_api_key(prov),
            base_url=openai_base_url(prov),
        )
    if prov == "custom":
        # Match langgraph/autogen: unauthenticated OpenAI-compat servers need a
        # non-empty key string for the client library.
        kwargs: dict = {
            "default_model": model,
            "api_key": openai_api_key(prov) or UNAUTHENTICATED_API_KEY,
            "base_url": openai_base_url(prov),
        }
        if apply_effort:
            kwargs["reasoning_effort"] = effort
        return OpenAISettings(**kwargs)
    kwargs = {
        "default_model": model,
        "api_key": os.environ.get(ENV_OPENAI_API_KEY) or None,
        "base_url": os.environ.get(ENV_OPENAI_BASE_URL) or None,
    }
    if apply_effort:
        kwargs["reasoning_effort"] = effort
    return OpenAISettings(**kwargs)


def _anthropic_settings_for_provider(model: str) -> AnthropicSettings:
    """Build Anthropic settings with an optional compatible gateway URL."""
    base = os.environ.get(ENV_ANTHROPIC_BASE_URL) or None
    return AnthropicSettings(
        default_model=model,
        api_key=os.environ.get(ENV_ANTHROPIC_API_KEY) or None,
        base_url=base,
    )


def build_mcp_request_params(
    *,
    model: str,
    max_steps: int,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
    provider: str,
    sampling: dict[str, float | int] | None = None,
):
    """Build mcp-agent ``RequestParams`` with provider-appropriate effort wiring.

    OpenAI/custom: ``reasoning_effort`` field.
    Anthropic: ``metadata.output_config.effort`` (merged into messages.create).
    DeepSeek: omit.
    *sampling* (temperature, top_p, seed) is sent as set; Anthropic and
    DeepSeek take no seed.
    """
    from mcp_agent.workflows.llm.augmented_llm import RequestParams

    effort = _mcp_reasoning_effort(reasoning_effort)
    kwargs: dict = {
        "model": model,
        "max_iterations": max_steps,
        "use_history": False,
    }
    if max_tokens is not None:
        kwargs["maxTokens"] = max_tokens
    prov = require_provider(provider)
    # mcp-agent merges ``metadata`` into the request, so unset sampling values
    # stay out of it and the server applies its own defaults.
    sampling = dict(sampling or {})
    if prov in ("anthropic", "deepseek"):
        sampling.pop("seed", None)
    if sampling:
        kwargs["metadata"] = sampling
    if effort is not None and prov == "anthropic":
        # Anthropic rejects "none"; omit output_config in that case.
        meta = anthropic_output_config(effort)
        if meta is not None:
            kwargs["metadata"] = {**kwargs.get("metadata", {}), **meta}
    elif effort is not None and prov != "deepseek":
        kwargs["reasoning_effort"] = effort
    return RequestParams(**kwargs)


def build_mcp_agent_settings(
    model: str,
    *,
    provider: str,
    reasoning_effort: str | None = None,
) -> Settings:
    """Build mcp-agent Settings for the task's troubleshooting session."""
    servers = session_mcp_config()
    prov = require_provider(provider)
    common = dict(
        execution_engine="asyncio",
        mcp=MCPSettings(
            servers={name: _to_server_settings(srv) for name, srv in servers.items()}
        ),
    )
    if prov == "anthropic":
        return Settings(
            **common,
            anthropic=_anthropic_settings_for_provider(model),
        )
    return Settings(
        **common,
        openai=_openai_settings_for_provider(
            model, provider, reasoning_effort=reasoning_effort
        ),
    )
