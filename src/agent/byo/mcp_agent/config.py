"""Bridge NIKA MCP server config to mcp-agent Settings."""

from __future__ import annotations

import os

from mcp_agent.config import MCPServerSettings, MCPSettings, OpenAISettings, Settings

from agent.utils.mcp_client import load_session_mcp_config
from agent.utils.mcp_servers import mcp_read_timeout_seconds, select_diagnosis_servers
from nika.utils.provider_env import (
    DEEPSEEK_OPENAI_BASE_URL,
    ENV_DEEPSEEK_API_KEY,
    ENV_OPENAI_API_KEY,
    ENV_OPENAI_BASE_URL,
    resolve_custom_api_key,
    resolve_custom_base_url,
)


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


def _openai_settings_for_provider(model: str, provider: str | None) -> OpenAISettings:
    prov = (provider or os.environ.get("NIKA_LLM_PROVIDER") or "openai").strip().lower()
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
        return OpenAISettings(
            default_model=model,
            api_key=key,
            base_url=base,
        )
    return OpenAISettings(
        default_model=model,
        api_key=os.environ.get(ENV_OPENAI_API_KEY) or None,
        base_url=os.environ.get(ENV_OPENAI_BASE_URL) or None,
    )


def build_mcp_agent_settings(
    session_id: str,
    scenario_name: str,
    model: str,
    *,
    provider: str | None = None,
) -> Settings:
    """Build mcp-agent Settings for a NIKA troubleshooting session."""
    servers = load_session_mcp_config(session_id, scenario_name)

    return Settings(
        execution_engine="asyncio",
        mcp=MCPSettings(
            servers={name: _to_server_settings(srv) for name, srv in servers.items()}
        ),
        openai=_openai_settings_for_provider(model, provider),
    )


def session_server_names(scenario_name: str) -> list[str]:
    from agent.utils.mcp_servers import select_session_servers

    return select_session_servers(scenario_name)


def diagnosis_server_names(scenario_name: str) -> list[str]:
    return select_diagnosis_servers(scenario_name)
