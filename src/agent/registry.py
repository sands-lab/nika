"""Agent type registry used by ``nika agent run``."""

import os
from typing import Any

from agent.sandbox.config import ENV_SANDBOX_EXECUTION

SANDBOX_AGENT_TYPES = frozenset(
    {
        "cli.codex",
        "cli.claude",
        "sdk.codex_sdk",
        "sdk.claude_sdk",
        "community.sade",
    }
)

_PROVIDER_REQUIRED = frozenset(
    {
        "byo.langgraph",
        "byo.mcp_agent",
        "byo.autogen",
        "cli.codex",
        "cli.claude",
        "sdk.codex_sdk",
        "sdk.claude_sdk",
        "community.sade",
    }
)


def create_agent(
    agent_type: str,
    *,
    session_id: str,
    model: str,
    llm_provider: str | None = None,
    max_steps: int = 20,
    reasoning_effort: str | None = None,
    stream_output: bool = True,
) -> Any:
    """Instantiate an agent for ``agent_type``."""
    normalized_type = agent_type.lower()
    if (
        normalized_type in SANDBOX_AGENT_TYPES
        and os.environ.get(ENV_SANDBOX_EXECUTION) != "1"
    ):
        raise RuntimeError(
            f"Agent {agent_type!r} can only run inside the Docker sandbox"
        )

    if normalized_type in _PROVIDER_REQUIRED and not llm_provider:
        raise ValueError(
            f"{agent_type} requires an LLM provider: set agent.provider in "
            "config/nika.yaml or pass -p/--provider."
        )

    match normalized_type:
        case "byo.langgraph":
            from agent.byo.langgraph.react_agent import BasicReActAgent

            return BasicReActAgent(
                session_id=session_id,
                llm_provider=llm_provider,
                model=model,
                max_steps=max_steps,
                reasoning_effort=reasoning_effort,
            )
        case "mock":
            from agent.mock.mock_agent import MockAgent

            return MockAgent(
                session_id=session_id,
                model=model,
                max_steps=max_steps,
            )
        case "sdk.claude_sdk":
            from agent.sdk.claude_sdk.agent import ClaudeSdkAgent

            return ClaudeSdkAgent(
                session_id=session_id,
                model=model,
                llm_provider=llm_provider,
                max_steps=max_steps,
                stream_output=stream_output,
            )
        case "sdk.codex_sdk":
            from agent.sdk.codex_sdk.agent import CodexSdkAgent

            return CodexSdkAgent(
                session_id=session_id,
                model=model,
                llm_provider=llm_provider,
                reasoning_effort=reasoning_effort,
                stream_output=stream_output,
            )
        case "cli.codex":
            from agent.cli.codex.agent import CodexCliAgent

            return CodexCliAgent(
                session_id=session_id,
                model=model,
                llm_provider=llm_provider,
                reasoning_effort=reasoning_effort,
                stream_output=stream_output,
            )
        case "cli.claude":
            from agent.cli.claude.agent import ClaudeAgent

            return ClaudeAgent(
                session_id=session_id,
                model=model,
                llm_provider=llm_provider,
                stream_output=stream_output,
            )
        case "byo.mcp_agent":
            from agent.byo.mcp_agent.agent import McpAgent

            return McpAgent(
                session_id=session_id,
                model=model,
                llm_provider=llm_provider,
                max_steps=max_steps,
                reasoning_effort=reasoning_effort,
                stream_output=stream_output,
            )
        case "byo.autogen":
            from agent.byo.autogen.agent import AutogenAgent

            return AutogenAgent(
                session_id=session_id,
                model=model,
                llm_provider=llm_provider,
                max_steps=max_steps,
                reasoning_effort=reasoning_effort,
                stream_output=stream_output,
            )
        case "community.sade":
            from agent.community.sade.agent import SadeAgent

            return SadeAgent(
                session_id=session_id,
                model=model,
                llm_provider=llm_provider,
                max_steps=max_steps,
            )
        case _:
            raise ValueError(f"Unsupported agent type: {agent_type!r}")
