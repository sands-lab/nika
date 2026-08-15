"""Unit tests for LangChain model factory provider wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.llm.model_factory import load_model


def test_load_model_anthropic_wires_chat_anthropic() -> None:
    fake = MagicMock(name="ChatAnthropic")
    with (
        patch.dict(
            "os.environ",
            {
                "ANTHROPIC_API_KEY": "sk-ant",
                "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
            },
            clear=False,
        ),
        patch("agent.llm.model_factory.ChatAnthropic", return_value=fake) as ctor,
    ):
        model = load_model(llm_provider="anthropic", model="deepseek-v4-flash")

    assert model is fake
    kwargs = ctor.call_args.kwargs
    assert kwargs["model"] == "deepseek-v4-flash"
    assert kwargs["api_key"] == "sk-ant"
    assert kwargs["base_url"] == "https://api.deepseek.com/anthropic"
    assert "default_request_timeout" in kwargs
    assert "max_retries" in kwargs


def test_load_model_anthropic_omits_empty_base_url() -> None:
    fake = MagicMock(name="ChatAnthropic")
    with (
        patch.dict(
            "os.environ",
            {"ANTHROPIC_API_KEY": "sk-ant", "ANTHROPIC_BASE_URL": ""},
            clear=False,
        ),
        patch("agent.llm.model_factory.ChatAnthropic", return_value=fake) as ctor,
    ):
        load_model(llm_provider="anthropic", model="claude-haiku-4-5")

    assert ctor.call_args.kwargs["base_url"] is None


def test_load_model_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported llm provider"):
        load_model(llm_provider="nope", model="x")
