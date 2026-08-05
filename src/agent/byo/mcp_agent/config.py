"""Bridge NIKA MCP server config to mcp-agent Settings."""

from __future__ import annotations

from mcp_agent.config import MCPServerSettings, MCPSettings, OpenAISettings, Settings

from agent.utils.mcp_client import load_session_mcp_config
from agent.utils.mcp_servers import select_diagnosis_servers


def _to_server_settings(server: dict) -> MCPServerSettings:
    transport = server.get("transport", "stdio")
    if transport == "http":
        return MCPServerSettings(
            transport="streamable_http",
            url=server["url"],
            headers=dict(server.get("headers") or {}),
        )
    return MCPServerSettings(
        transport=server.get("transport", "stdio"),
        command=server["command"],
        args=server.get("args", []),
        env=server.get("env"),
    )


def build_mcp_agent_settings(
    session_id: str,
    scenario_name: str,
    model: str,
) -> Settings:
    """Build mcp-agent Settings for a NIKA troubleshooting session."""
    servers = load_session_mcp_config(session_id, scenario_name)

    return Settings(
        execution_engine="asyncio",
        mcp=MCPSettings(servers={name: _to_server_settings(srv) for name, srv in servers.items()}),
        openai=OpenAISettings(default_model=model),
    )


def session_server_names(scenario_name: str) -> list[str]:
    from agent.utils.mcp_servers import select_session_servers

    return select_session_servers(scenario_name)


def diagnosis_server_names(scenario_name: str) -> list[str]:
    return select_diagnosis_servers(scenario_name)
