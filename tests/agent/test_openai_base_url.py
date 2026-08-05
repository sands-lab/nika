"""Unit tests: OPENAI_BASE_URL wiring for BYO OpenAI-compatible agents."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mcp_agent.config import OpenAISettings

from agent.byo.autogen.runner import create_model_client


def test_mcp_agent_openai_settings_reads_openai_base_url(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-or-test")

    settings = OpenAISettings(default_model="openai/gpt-4o-mini")

    assert settings.base_url == "https://openrouter.ai/api/v1"
    assert settings.api_key == "sk-or-test"
    assert settings.default_model == "openai/gpt-4o-mini"


def test_autogen_create_model_client_uses_openai_base_url(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-or-test")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    fake = MagicMock(name="client")
    with patch(
        "agent.byo.autogen.runner.OpenAIChatCompletionClient", return_value=fake
    ) as ctor:
        client = create_model_client("openai/gpt-4o-mini")

    assert client is fake
    kwargs = ctor.call_args.kwargs
    assert kwargs["model"] == "openai/gpt-4o-mini"
    assert kwargs["model_info"]["function_calling"] is True
    # Base URL / key come from the OpenAI SDK env vars, not explicit kwargs.
    assert "base_url" not in kwargs
