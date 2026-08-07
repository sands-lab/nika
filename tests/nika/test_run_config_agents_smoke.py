"""Smoke: each agent type resolves type/provider/model/max_steps from YAML+CLI."""

from __future__ import annotations

import pytest

from nika.run_config.loader import merge_cli, reset_run_config, set_run_config
from nika.run_config.schema import RunConfig
from nika.utils.agent_config import (
    resolve_agent_model,
    resolve_agent_type,
    resolve_llm_provider,
    resolve_max_steps,
)

AGENT_SPECS = (
    ("byo.langgraph", "deepseek", "deepseek-chat"),
    ("byo.mcp_agent", "deepseek", "deepseek-chat"),
    ("byo.autogen", "deepseek", "deepseek-chat"),
    ("cli.claude", "deepseek", "deepseek-v4-pro[1m]"),
    ("cli.codex", "openai", "gpt-5.4-mini"),
    ("sdk.claude_sdk", "deepseek", "deepseek-v4-flash"),
    ("sdk.codex_sdk", "openai", "gpt-5.4-mini"),
    ("community.sade", "deepseek", "deepseek-v4-flash"),
)


@pytest.fixture(autouse=True)
def _isolate_run_config():
    reset_run_config()
    yield
    reset_run_config()


@pytest.mark.parametrize("agent_type,provider,model", AGENT_SPECS)
def test_agent_resolves_from_yaml_and_cli(
    agent_type: str, provider: str, model: str
) -> None:
    base = RunConfig.model_validate(
        {
            "agent": {
                "type": "byo.langgraph",
                "provider": "openai",
                "max_steps": 7,
                "models": {
                    "langgraph": "gpt-5-mini",
                    "mcp_agent": "gpt-4.1-mini",
                    "autogen": "gpt-4.1-mini",
                    "codex": "gpt-5.4-mini",
                    "claude": "claude-sonnet",
                },
            }
        }
    )
    cfg = merge_cli(
        base,
        agent_type=agent_type,
        llm_provider=provider,
        model=model,
        max_steps=11,
    )
    set_run_config(cfg)

    assert resolve_agent_type(None) == agent_type
    assert resolve_llm_provider(None, agent_type=agent_type) == provider
    assert resolve_agent_model(agent_type, None, llm_provider=provider) == model
    assert resolve_max_steps(None) == 11
