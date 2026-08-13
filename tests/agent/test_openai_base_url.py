"""Unit tests: OpenAI-compatible base URL wiring for BYO agents."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agent.byo.autogen.runner import create_model_client
from agent.byo.mcp_agent.config import _openai_settings_for_provider


def test_mcp_agent_openai_settings_reads_openai_base_url(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-or-test")

    settings = _openai_settings_for_provider("openai/gpt-4o-mini", "openai")

    assert settings.base_url == "https://openrouter.ai/api/v1"
    assert settings.api_key == "sk-or-test"
    assert settings.default_model == "openai/gpt-4o-mini"


def test_mcp_agent_custom_provider_uses_nika_custom(monkeypatch) -> None:
    from nika.run_config.loader import reset_run_config, set_run_config
    from nika.run_config.schema import RunConfig
    from nika.utils.agent_config import apply_custom_provider_env

    monkeypatch.setenv("NIKA_CUSTOM_API_KEY", "sk-or-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("NIKA_CUSTOM_BASE_URL", raising=False)
    set_run_config(
        RunConfig.model_validate(
            {
                "agent": {
                    "provider": "custom",
                    "custom": {"base_url": "https://openrouter.ai/api/v1"},
                }
            }
        )
    )
    apply_custom_provider_env()
    try:
        settings = _openai_settings_for_provider("openai/gpt-4o-mini", "custom")
    finally:
        reset_run_config()

    assert settings.base_url == "https://openrouter.ai/api/v1"
    assert settings.api_key == "sk-or-test"


def test_autogen_create_model_client_uses_custom(monkeypatch) -> None:
    from nika.run_config.loader import reset_run_config, set_run_config
    from nika.run_config.schema import RunConfig
    from nika.utils.agent_config import apply_custom_provider_env

    monkeypatch.setenv("NIKA_CUSTOM_API_KEY", "sk-or-test")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("NIKA_CUSTOM_BASE_URL", raising=False)
    set_run_config(
        RunConfig.model_validate(
            {
                "agent": {
                    "provider": "custom",
                    "custom": {"base_url": "https://openrouter.ai/api/v1"},
                }
            }
        )
    )
    apply_custom_provider_env()
    try:
        fake = MagicMock(name="client")
        with patch(
            "agent.byo.autogen.runner.OpenAIChatCompletionClient", return_value=fake
        ) as ctor:
            client = create_model_client("openai/gpt-4o-mini", provider="custom")

        assert client is fake
        kwargs = ctor.call_args.kwargs
        assert kwargs["model"] == "openai/gpt-4o-mini"
        assert kwargs["base_url"] == "https://openrouter.ai/api/v1"
        assert kwargs["api_key"] == "sk-or-test"
    finally:
        reset_run_config()


def test_autogen_deepseek_provider(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    fake = MagicMock(name="client")
    with patch(
        "agent.byo.autogen.runner.OpenAIChatCompletionClient", return_value=fake
    ) as ctor:
        create_model_client("deepseek-chat", provider="deepseek")

    kwargs = ctor.call_args.kwargs
    assert kwargs["api_key"] == "sk-ds"
    assert kwargs["base_url"] == "https://api.deepseek.com"
