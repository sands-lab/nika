from __future__ import annotations

import pytest

from nika.run_config.loader import reset_run_config, set_run_config
from nika.run_config.schema import RunConfig
from nika.utils.agent_config import (
    resolve_agent_model,
    resolve_agent_type,
    resolve_judge_model,
    resolve_judge_provider,
    resolve_llm_provider,
    resolve_max_steps,
    resolve_reasoning_effort,
)


@pytest.fixture(autouse=True)
def _clear_run_config():
    reset_run_config()
    yield
    reset_run_config()


class AgentConfigTest:
    def test_required_from_config(self) -> None:
        set_run_config(
            RunConfig.model_validate(
                {
                    "agent": {
                        "type": "byo.langgraph",
                        "provider": "openai",
                        "max_steps": 20,
                        "models": {"langgraph": "gpt-5-mini"},
                    },
                    "nika": {
                        "judge": {"provider": "deepseek", "model": "deepseek-chat"}
                    },
                }
            )
        )
        assert resolve_agent_type(None) == "byo.langgraph"
        assert resolve_max_steps(None) == 20
        assert resolve_judge_provider(None) == "deepseek"
        assert resolve_judge_model(None) == "deepseek-chat"

    def test_mock_agent_does_not_require_llm_provider(self) -> None:
        set_run_config(RunConfig())
        assert resolve_llm_provider(None, agent_type="mock") is None

    def test_provider_validated_per_agent(self) -> None:
        with pytest.raises(ValueError, match="not supported"):
            resolve_llm_provider("openai", agent_type="cli.claude")
        assert resolve_llm_provider("deepseek", agent_type="cli.claude") == "deepseek"

    def test_reasoning_effort_from_config(self) -> None:
        set_run_config(
            RunConfig.model_validate(
                {
                    "agent": {
                        "type": "cli.codex",
                        "provider": "openai",
                        "reasoning_effort": "medium",
                    }
                }
            )
        )
        assert resolve_reasoning_effort(None) == "medium"
        assert resolve_reasoning_effort("high") == "high"

    def test_agent_specific_model_from_config(self) -> None:
        cases = [
            ("byo.langgraph", {"langgraph": "deepseek-chat"}),
            ("byo.mcp_agent", {"mcp_agent": "gpt-4.1-mini"}),
            ("byo.autogen", {"autogen": "deepseek-chat"}),
            ("cli.codex", {"codex": "gpt-5.4-mini"}),
            ("sdk.codex_sdk", {"codex_sdk": "gpt-5.4-mini"}),
            ("sdk.claude_sdk", {"claude_sdk": "deepseek-v4-flash"}),
            ("community.sade", {"sade": "deepseek-v4-flash"}),
            ("cli.claude", {"claude": "deepseek-v4-pro[1m]"}),
        ]
        for agent_type, models in cases:
            set_run_config(
                RunConfig.model_validate(
                    {
                        "agent": {
                            "type": agent_type,
                            "provider": (
                                "deepseek"
                                if "claude" in agent_type
                                or agent_type == "community.sade"
                                else "openai"
                            ),
                            "models": models,
                        }
                    }
                )
            )
            assert resolve_agent_model(agent_type, None) == next(iter(models.values()))

    def test_custom_model_fallback(self) -> None:
        set_run_config(
            RunConfig.model_validate(
                {
                    "agent": {
                        "type": "byo.langgraph",
                        "provider": "custom",
                        "custom": {
                            "base_url": "http://localhost:11434/v1",
                            "model": "qwen2.5:7b",
                        },
                    }
                }
            )
        )
        assert (
            resolve_agent_model("byo.langgraph", None, llm_provider="custom")
            == "qwen2.5:7b"
        )
