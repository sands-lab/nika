from __future__ import annotations

import pytest
import os
import unittest.mock
from nika.utils.agent_config import (
    ENV_AGENT_TYPE,
    ENV_AUTOGEN_MODEL,
    ENV_CLAUDE_SDK_MODEL,
    ENV_CODEX_MODEL,
    ENV_CODEX_SDK_MODEL,
    ENV_JUDGE_MODEL,
    ENV_JUDGE_PROVIDER,
    ENV_LANGGRAPH_MODEL,
    ENV_LLM_PROVIDER,
    ENV_MAX_STEPS,
    ENV_MCP_AGENT_MODEL,
    ENV_MODEL,
    ENV_SADE_MODEL,
    resolve_agent_model,
    resolve_agent_type,
    resolve_judge_model,
    resolve_judge_provider,
    resolve_llm_provider,
    resolve_max_steps,
    resolve_reasoning_effort,
)
from tests.support.integration_pipeline import load_test_env

load_test_env()


class AgentConfigTest:
    def test_cli_values_override_env(self) -> None:
        with unittest.mock.patch.dict(
            os.environ,
            {
                ENV_AGENT_TYPE: "mock",
                ENV_LANGGRAPH_MODEL: "deepseek-chat",
                ENV_LLM_PROVIDER: "deepseek",
                ENV_MAX_STEPS: "99",
            },
            clear=True,
        ):
            assert resolve_agent_type("byo.langgraph") == "byo.langgraph"
            assert (
                resolve_llm_provider("openai", agent_type="byo.langgraph") == "openai"
            )
            assert resolve_max_steps(20) == 20
            assert resolve_agent_model("byo.langgraph", "override") == "override"

    def test_required_shared_values_raise_when_missing(self) -> None:
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            for resolver in (
                resolve_agent_type,
                resolve_max_steps,
                resolve_judge_provider,
            ):
                with pytest.raises(ValueError):
                    resolver(None)
            with pytest.raises(ValueError):
                resolve_llm_provider(None, agent_type="byo.langgraph")

    def test_non_langgraph_agents_do_not_require_llm_provider(self) -> None:
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            assert resolve_llm_provider(None, agent_type="mock") is None
            assert resolve_llm_provider(None, agent_type="cli.codex") is None

    def test_judge_values_from_env(self) -> None:
        with unittest.mock.patch.dict(
            os.environ,
            {ENV_JUDGE_PROVIDER: "deepseek", ENV_JUDGE_MODEL: "deepseek-chat"},
            clear=True,
        ):
            assert resolve_judge_provider(None) == "deepseek"
            assert resolve_judge_model(None) == "deepseek-chat"

    def test_reasoning_effort_from_env(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {"NIKA_CODEX_REASONING_EFFORT": "medium"}, clear=True
        ):
            assert resolve_reasoning_effort(None) == "medium"
            assert resolve_reasoning_effort("high") == "high"

    def test_agent_specific_model_envs(self) -> None:
        cases = [
            ("mock", ENV_MODEL, "mock-v1"),
            ("byo.langgraph", ENV_LANGGRAPH_MODEL, "deepseek-chat"),
            ("byo.mcp_agent", ENV_MCP_AGENT_MODEL, "gpt-4.1-mini"),
            ("byo.autogen", ENV_AUTOGEN_MODEL, "deepseek-chat"),
            ("cli.codex", ENV_CODEX_MODEL, "gpt-5.4-mini"),
            ("sdk.codex_sdk", ENV_CODEX_MODEL, "gpt-5.4-mini"),
            ("sdk.codex_sdk", ENV_CODEX_SDK_MODEL, "gpt-5.4-mini"),
            ("sdk.claude_sdk", ENV_CLAUDE_SDK_MODEL, "deepseek-v4-flash"),
            ("community.sade", ENV_SADE_MODEL, "deepseek-v4-flash"),
        ]
        for agent_type, env_key, model in cases:
            with unittest.mock.patch.dict(os.environ, {env_key: model}, clear=True):
                assert resolve_agent_model(agent_type, None) == model

    def test_claude_family_falls_back_to_anthropic_model(self) -> None:
        for agent_type in ("cli.claude", "sdk.claude_sdk", "community.sade"):
            with unittest.mock.patch.dict(
                os.environ, {"ANTHROPIC_MODEL": "deepseek-v4-pro[1m]"}, clear=True
            ):
                assert resolve_agent_model(agent_type, None) == "deepseek-v4-pro[1m]"
