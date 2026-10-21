from __future__ import annotations


import pytest

from nika.agents.registry import create_agent

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "agent_type", ["cli.codex", "byo.langgraph", "sdk.claude_sdk", "community.sade"]
)
def test_sandboxed_agents_cannot_run_on_host(agent_type: str) -> None:
    with pytest.raises(RuntimeError, match="runs only inside its sandbox"):
        create_agent(agent_type, session_id="test-session", model="test-model")


def test_unknown_agent_still_reports_unsupported_type() -> None:
    with pytest.raises(ValueError, match="Unsupported agent type"):
        create_agent("unknown", session_id="test-session", model="test-model")
