"""Agent type registry used by ``nika agent run``."""

import asyncio
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


class AgentTimeoutError(RuntimeError):
    """The agent run exceeded ``agent.timeout_sec``."""


def run_agent(agent: Any, task_description: str, *, timeout_sec: int) -> Any:
    """Run ``agent.run`` to completion within the shared ``agent.timeout_sec`` budget.

    ``timeout_sec <= 0`` disables the budget.
    """

    async def _run() -> Any:
        if timeout_sec <= 0:
            return await agent.run(task_description=task_description)
        try:
            async with asyncio.timeout(timeout_sec) as budget:
                return await agent.run(task_description=task_description)
        except TimeoutError:
            # A TimeoutError raised by the agent itself (e.g. an LLM request)
            # is not a budget expiry; keep it for outcome classification.
            if not budget.expired():
                raise
        # Raised outside the handler so the chain carries no TimeoutError, which
        # outcome classification would read as an LLM endpoint timeout.
        raise AgentTimeoutError(
            f"agent run exceeded agent.timeout_sec ({timeout_sec}s)"
        )

    return asyncio.run(_run())


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
                stream_output=stream_output,
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
                max_steps=max_steps,
                reasoning_effort=reasoning_effort,
                stream_output=stream_output,
            )
        case "cli.codex":
            from agent.cli.codex.agent import CodexCliAgent

            return CodexCliAgent(
                session_id=session_id,
                model=model,
                llm_provider=llm_provider,
                max_steps=max_steps,
                reasoning_effort=reasoning_effort,
                stream_output=stream_output,
            )
        case "cli.claude":
            from agent.cli.claude.agent import ClaudeAgent

            return ClaudeAgent(
                session_id=session_id,
                model=model,
                llm_provider=llm_provider,
                max_steps=max_steps,
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
                stream_output=stream_output,
            )
        case _:
            raise ValueError(f"Unsupported agent type: {agent_type!r}")
