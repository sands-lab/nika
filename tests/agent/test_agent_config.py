from __future__ import annotations

import warnings

import pytest

from nika.run_config.loader import reset_run_config, set_run_config
from nika.run_config.schema import RunConfig
from nika.utils.agent_config import (
    apply_custom_provider_env,
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
                        "model": "deepseek-v4-flash",
                        "max_steps": 20,
                    },
                    "nika": {
                        "judge": {
                            "provider": "deepseek",
                            "model": "deepseek-v4-flash",
                        }
                    },
                }
            )
        )
        assert resolve_agent_type(None) == "byo.langgraph"
        assert resolve_max_steps(None) == 20
        assert resolve_judge_provider(None) == "deepseek"
        assert resolve_judge_model(None) == "deepseek-v4-flash"
        assert resolve_agent_model("byo.langgraph", None) == "deepseek-v4-flash"

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

    def test_agent_model_from_config(self) -> None:
        cases = [
            ("byo.langgraph", "deepseek-v4-flash"),
            ("byo.mcp_agent", "deepseek-v4-flash"),
            ("byo.autogen", "deepseek-v4-flash"),
            ("cli.codex", "gpt-5-mini"),
            ("sdk.codex_sdk", "gpt-5-mini"),
            ("sdk.claude_sdk", "deepseek-v4-flash"),
            ("community.sade", "deepseek-v4-flash"),
            ("cli.claude", "deepseek-v4-flash"),
        ]
        for agent_type, model in cases:
            provider = (
                "openai"
                if agent_type in ("cli.codex", "sdk.codex_sdk")
                else "deepseek"
            )
            set_run_config(
                RunConfig.model_validate(
                    {
                        "agent": {
                            "type": agent_type,
                            "provider": provider,
                            "model": model,
                        }
                    }
                )
            )
            assert resolve_agent_model(agent_type, None) == model

    def test_legacy_models_field_fallback(self) -> None:
        set_run_config(
            RunConfig.model_validate(
                {
                    "agent": {
                        "type": "byo.langgraph",
                        "provider": "openai",
                        "models": {"langgraph": "legacy-model"},
                    }
                }
            )
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert resolve_agent_model("byo.langgraph", None) == "legacy-model"
        assert any(
            issubclass(w.category, DeprecationWarning) and "agent.models" in str(w.message)
            for w in caught
        )

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

    def test_custom_model_wins_over_legacy_models(self) -> None:
        set_run_config(
            RunConfig.model_validate(
                {
                    "agent": {
                        "type": "byo.langgraph",
                        "provider": "custom",
                        "models": {"langgraph": "gpt-5-mini"},
                        "custom": {
                            "base_url": "http://localhost:11434/v1",
                            "model": "otel-31b",
                        },
                    }
                }
            )
        )
        assert (
            resolve_agent_model("byo.langgraph", None, llm_provider="custom")
            == "otel-31b"
        )

    def test_agent_model_wins_over_legacy_models_for_custom(self) -> None:
        set_run_config(
            RunConfig.model_validate(
                {
                    "agent": {
                        "type": "byo.langgraph",
                        "provider": "custom",
                        "model": "otel-31b",
                        "models": {"langgraph": "gpt-5-mini"},
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
            == "otel-31b"
        )

    def test_apply_custom_provider_env_exports_agent_model(self, monkeypatch) -> None:
        for key in ("NIKA_CUSTOM_MODEL", "NIKA_CUSTOM_BASE_URL", "CUSTOM_API_BASE"):
            monkeypatch.delenv(key, raising=False)
        cfg = RunConfig.model_validate(
            {
                "agent": {
                    "type": "byo.langgraph",
                    "provider": "custom",
                    "model": "otel-31b",
                    "custom": {"base_url": "http://example.com/v1"},
                }
            }
        )
        apply_custom_provider_env(cfg)
        import os

        assert os.environ["NIKA_CUSTOM_MODEL"] == "otel-31b"
        assert os.environ["NIKA_CUSTOM_BASE_URL"] == "http://example.com/v1"
        # Drop process env without monkeypatch.delenv (that would restore the
        # just-set values at teardown and leak into later provider tests).
        for key in ("NIKA_CUSTOM_MODEL", "NIKA_CUSTOM_BASE_URL", "CUSTOM_API_BASE"):
            os.environ.pop(key, None)
