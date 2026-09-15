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
    resolve_reasoning_effort,
)

DEEPSEEK_FLASH = "deepseek-v4-flash"

AGENT_SPECS = (
    ("byo.langgraph", "deepseek", DEEPSEEK_FLASH),
    ("byo.mcp_agent", "deepseek", DEEPSEEK_FLASH),
    ("byo.autogen", "deepseek", DEEPSEEK_FLASH),
    ("cli.claude", "deepseek", DEEPSEEK_FLASH),
    ("cli.codex", "openai", "gpt-5-mini"),
    ("sdk.claude_sdk", "deepseek", DEEPSEEK_FLASH),
    ("sdk.codex_sdk", "openai", "gpt-5-mini"),
    ("community.sade", "deepseek", DEEPSEEK_FLASH),
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
                "provider": "deepseek",
                "model": DEEPSEEK_FLASH,
                "max_steps": 7,
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


@pytest.mark.parametrize("agent_type,provider,model", AGENT_SPECS)
def test_agent_resolves_from_yaml_only(
    agent_type: str, provider: str, model: str
) -> None:
    set_run_config(
        RunConfig.model_validate(
            {
                "agent": {
                    "type": agent_type,
                    "provider": provider,
                    "model": model,
                    "max_steps": 9,
                    "reasoning_effort": "low",
                }
            }
        )
    )

    assert resolve_agent_type(None) == agent_type
    assert resolve_llm_provider(None, agent_type=agent_type) == provider
    assert resolve_agent_model(agent_type, None, llm_provider=provider) == model
    assert resolve_max_steps(None) == 9
    if agent_type in ("byo.langgraph", "cli.codex", "sdk.codex_sdk"):
        assert resolve_reasoning_effort(None) == "low"
