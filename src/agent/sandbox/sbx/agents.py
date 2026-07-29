"""Map NIKA agent types to native Docker Sandboxes agents."""

from __future__ import annotations

NATIVE_SBX_AGENTS: dict[str, str] = {
    "cli.codex": "codex",
    "cli.claude": "claude",
    "sdk.codex_sdk": "shell",
    "sdk.claude_sdk": "shell",
    "community.sade": "shell",
}

ENV_SBX_SANDBOX_NAME = "NIKA_SBX_SANDBOX_NAME"


def native_sbx_agent(agent_type: str) -> str:
    try:
        return NATIVE_SBX_AGENTS[agent_type]
    except KeyError as exc:
        raise ValueError(f"No native sbx agent for {agent_type!r}") from exc


def uses_native_sbx_agent(agent_type: str) -> bool:
    return agent_type in NATIVE_SBX_AGENTS
