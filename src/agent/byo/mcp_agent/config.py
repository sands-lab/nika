"""Bridge NIKA MCP server config to mcp-agent Settings."""

from __future__ import annotations

import os

from mcp_agent.config import (
    AnthropicSettings,
    MCPServerSettings,
    MCPSettings,
    OpenAISettings,
    Settings,
)

from agent.utils.mcp_client import load_session_mcp_config
from agent.utils.mcp_servers import mcp_read_timeout_seconds, select_diagnosis_servers
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


def _resolve_provider(provider: str | None) -> str:
    return (provider or os.environ.get("NIKA_LLM_PROVIDER") or "openai").strip().lower()


def _openai_settings_for_provider(
    model: str,
    provider: str | None,
    *,
    reasoning_effort: str | None = None,
) -> OpenAISettings:
    prov = _resolve_provider(provider)
    effort = _mcp_reasoning_effort(reasoning_effort)
    # DeepSeek OpenAI-compat does not take reasoning_effort.
    apply_effort = effort is not None and prov != "deepseek"
    if prov == "deepseek":
        api_key = os.environ.get(ENV_DEEPSEEK_API_KEY) or os.environ.get(
            ENV_OPENAI_API_KEY
        )
        return OpenAISettings(
            default_model=model,
            api_key=api_key,
            base_url=os.environ.get(ENV_OPENAI_BASE_URL) or DEEPSEEK_OPENAI_BASE_URL,
        )
    if prov == "custom":
        base = resolve_custom_base_url() or os.environ.get(ENV_OPENAI_BASE_URL) or None
        key = resolve_custom_api_key() or os.environ.get(ENV_OPENAI_API_KEY) or None
        kwargs: dict = {
            "default_model": model,
            "api_key": key,
            "base_url": base,
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
    """Build Anthropic settings (honors optional ANTHROPIC_BASE_URL)."""
    return AnthropicSettings(
        default_model=model,
        api_key=os.environ.get(ENV_ANTHROPIC_API_KEY) or None,
        base_url=os.environ.get(ENV_ANTHROPIC_BASE_URL) or None,
    )


def build_mcp_request_params(
    *,
    model: str,
    max_steps: int,
    reasoning_effort: str | None = None,
    provider: str | None = None,
):
    """Build mcp-agent ``RequestParams`` with provider-appropriate effort wiring.

    OpenAI/custom: ``reasoning_effort`` field.
    Anthropic: ``metadata.output_config.effort`` (merged into messages.create).
    DeepSeek: omit.
    """
    from mcp_agent.workflows.llm.augmented_llm import RequestParams

    effort = _mcp_reasoning_effort(reasoning_effort)
    kwargs: dict = {
        "model": model,
        "max_iterations": max_steps,
        "temperature": 0,
        "use_history": False,
    }
    prov = _resolve_provider(provider)
    if effort is not None and prov == "anthropic":
        # Anthropic rejects "none"; omit output_config in that case.
        meta = anthropic_output_config(effort)
        if meta is not None:
            kwargs["metadata"] = meta
    elif effort is not None and prov != "deepseek":
        kwargs["reasoning_effort"] = effort
    return RequestParams(**kwargs)


def build_mcp_agent_settings(
    session_id: str,
    scenario_name: str,
    model: str,
    *,
    provider: str | None = None,
    reasoning_effort: str | None = None,
) -> Settings:
    """Build mcp-agent Settings for a NIKA troubleshooting session."""
    servers = load_session_mcp_config(session_id, scenario_name)
    prov = _resolve_provider(provider)
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


def session_server_names(scenario_name: str) -> list[str]:
    from agent.utils.mcp_servers import select_session_servers

    return select_session_servers(scenario_name)


def diagnosis_server_names(scenario_name: str) -> list[str]:
    return select_diagnosis_servers(scenario_name)
