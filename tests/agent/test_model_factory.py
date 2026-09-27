"""Unit tests for LangChain model factory provider wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import agent.llm.model_factory as model_factory
from agent.llm.model_factory import load_model


def test_provider_chat_classes_are_lazily_imported() -> None:
    assert not hasattr(model_factory, "ChatAnthropic")
    assert not hasattr(model_factory, "ChatDeepSeek")


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
        patch("langchain_anthropic.ChatAnthropic", return_value=fake) as ctor,
    ):
        model = load_model(llm_provider="anthropic", model="deepseek-v4-flash")

    assert model is fake
    kwargs = ctor.call_args.kwargs
    assert kwargs["model"] == "deepseek-v4-flash"
    assert kwargs["api_key"] == "sk-ant"
    assert kwargs["base_url"] == "https://api.deepseek.com/anthropic"
    assert "default_request_timeout" in kwargs
    assert "max_retries" in kwargs
    assert "reasoning_effort" not in kwargs


def test_load_model_anthropic_omits_empty_base_url() -> None:
    fake = MagicMock(name="ChatAnthropic")
    with (
        patch.dict(
            "os.environ",
            {"ANTHROPIC_API_KEY": "sk-ant", "ANTHROPIC_BASE_URL": ""},
            clear=False,
        ),
        patch("langchain_anthropic.ChatAnthropic", return_value=fake) as ctor,
    ):
        load_model(llm_provider="anthropic", model="claude-haiku-4-5")

    assert ctor.call_args.kwargs["base_url"] is None


def test_load_model_openai_passes_reasoning_effort() -> None:
    fake = MagicMock(name="ReasoningChatOpenAI")
    with patch(
        "agent.llm.model_factory.ReasoningChatOpenAI", return_value=fake
    ) as ctor:
        model = load_model(
            llm_provider="openai",
            model="gpt-5.6",
            reasoning_effort="medium",
            max_retries=2,
        )

    assert model is fake
    kwargs = ctor.call_args.kwargs
    assert kwargs["model_name"] == "gpt-5.6"
    assert kwargs["reasoning_effort"] == "medium"
    # Client-side retries off; NIKA logs each attempt via ``llm_retry``.
    assert kwargs["max_retries"] == 0
    assert fake._nika_max_retries == 2


def test_load_model_openai_omits_reasoning_effort_when_unset() -> None:
    fake = MagicMock(name="ReasoningChatOpenAI")
    with patch(
        "agent.llm.model_factory.ReasoningChatOpenAI", return_value=fake
    ) as ctor:
        load_model(llm_provider="openai", model="gpt-5-mini")

    assert "reasoning_effort" not in ctor.call_args.kwargs
    assert "max_tokens" not in ctor.call_args.kwargs
    assert ctor.call_args.kwargs["max_retries"] == 0


def test_load_model_openai_passes_max_tokens() -> None:
    with patch("agent.llm.model_factory.ReasoningChatOpenAI") as ctor:
        load_model(llm_provider="openai", model="gpt-5-mini", max_tokens=8192)

    assert ctor.call_args.kwargs["max_tokens"] == 8192


def test_load_model_custom_enables_thinking_extra_body() -> None:
    fake = MagicMock(name="ReasoningChatOpenAI")
    with (
        patch(
            "agent.llm.model_factory.resolve_custom_base_url",
            return_value="http://example:8000/v1",
        ),
        patch("agent.llm.model_factory.resolve_custom_api_key", return_value=None),
        patch("agent.llm.model_factory.ReasoningChatOpenAI", return_value=fake) as ctor,
    ):
        load_model(
            llm_provider="custom",
            model="qwen",
            reasoning_effort="low",
            max_retries=3,
        )

    kwargs = ctor.call_args.kwargs
    assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}
    assert "reasoning_effort" not in kwargs
    assert kwargs["max_retries"] == 0
    assert fake._nika_max_retries == 3


def test_load_model_custom_skips_thinking_extra_body_when_none() -> None:
    fake = MagicMock(name="ReasoningChatOpenAI")
    with (
        patch(
            "agent.llm.model_factory.resolve_custom_base_url",
            return_value="http://example:8000/v1",
        ),
        patch("agent.llm.model_factory.resolve_custom_api_key", return_value=None),
        patch("agent.llm.model_factory.ReasoningChatOpenAI", return_value=fake) as ctor,
    ):
        load_model(
            llm_provider="custom",
            model="qwen",
            reasoning_effort="none",
        )

    assert "extra_body" not in ctor.call_args.kwargs


def test_load_model_custom_sends_max_tokens_in_body() -> None:
    with (
        patch(
            "agent.llm.model_factory.resolve_custom_base_url",
            return_value="http://example:8000/v1",
        ),
        patch("agent.llm.model_factory.resolve_custom_api_key", return_value=None),
        patch("agent.llm.model_factory.ReasoningChatOpenAI") as ctor,
    ):
        load_model(
            llm_provider="custom",
            model="qwen",
            reasoning_effort="low",
            max_tokens=8192,
        )

    kwargs = ctor.call_args.kwargs
    assert kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": True},
        "max_tokens": 8192,
    }
    assert "max_tokens" not in kwargs


def test_load_model_anthropic_and_deepseek_pass_max_tokens() -> None:
    with (
        patch.dict(
            "os.environ",
            {"ANTHROPIC_API_KEY": "sk-ant", "DEEPSEEK_API_KEY": "sk-ds"},
            clear=False,
        ),
        patch("langchain_anthropic.ChatAnthropic") as anthropic,
        patch("langchain_deepseek.ChatDeepSeek") as deepseek,
    ):
        load_model(llm_provider="anthropic", model="claude-opus-4-6", max_tokens=8192)
        load_model(llm_provider="deepseek", model="deepseek-chat", max_tokens=8192)

    assert anthropic.call_args.kwargs["max_tokens"] == 8192
    assert deepseek.call_args.kwargs["max_tokens"] == 8192


def test_load_model_anthropic_passes_reasoning_effort() -> None:
    fake = MagicMock(name="ChatAnthropic")
    with (
        patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-ant"}, clear=False),
        patch("langchain_anthropic.ChatAnthropic", return_value=fake) as ctor,
    ):
        load_model(
            llm_provider="anthropic",
            model="claude-opus-4-6",
            reasoning_effort="high",
        )

    assert ctor.call_args.kwargs["reasoning_effort"] == "high"


def test_load_model_deepseek_ignores_reasoning_effort() -> None:
    fake = MagicMock(name="ChatDeepSeek")
    with (
        patch.dict("os.environ", {"DEEPSEEK_API_KEY": "sk-ds"}, clear=False),
        patch("langchain_deepseek.ChatDeepSeek", return_value=fake) as ctor,
    ):
        load_model(
            llm_provider="deepseek",
            model="deepseek-chat",
            reasoning_effort="medium",
        )

    assert "reasoning_effort" not in ctor.call_args.kwargs


def test_load_model_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported llm provider"):
        load_model(llm_provider="nope", model="x")
