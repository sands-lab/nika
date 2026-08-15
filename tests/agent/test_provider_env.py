"""Unit tests for provider credential mapping and subprocess allowlists."""

from __future__ import annotations

import os
import warnings
from unittest.mock import patch

import pytest

from agent.utils.provider_env import (
    DEEPSEEK_ANTHROPIC_BASE_URL,
    DEEPSEEK_OPENAI_BASE_URL,
    build_agent_subprocess_env,
    map_provider_credentials,
    provider_env_context,
    resolve_custom_base_url,
    validate_provider_for_agent,
)


def test_validate_provider_rejects_unsupported_combo() -> None:
    with pytest.raises(ValueError, match="not supported"):
        validate_provider_for_agent("cli.claude", "openai")
    with pytest.raises(ValueError, match="not supported"):
        validate_provider_for_agent("cli.codex", "anthropic")
    assert validate_provider_for_agent("byo.langgraph", "anthropic") == "anthropic"
    assert validate_provider_for_agent("byo.mcp_agent", "anthropic") == "anthropic"
    assert validate_provider_for_agent("byo.autogen", "anthropic") == "anthropic"


def test_anthropic_maps_credentials_for_byo_agents() -> None:
    for agent_type in ("byo.langgraph", "byo.mcp_agent", "byo.autogen"):
        mapped = map_provider_credentials(
            agent_type=agent_type,
            provider="anthropic",
            sources={
                "ANTHROPIC_API_KEY": "sk-ant",
                "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
            },
        )
        assert mapped["ANTHROPIC_API_KEY"] == "sk-ant"
        assert mapped["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
        assert "OPENAI_API_KEY" not in mapped
        assert "DEEPSEEK_API_KEY" not in mapped


def test_deepseek_maps_to_openai_compat_for_langgraph() -> None:
    mapped = map_provider_credentials(
        agent_type="byo.langgraph",
        provider="deepseek",
        sources={"DEEPSEEK_API_KEY": "sk-ds"},
    )
    assert mapped["DEEPSEEK_API_KEY"] == "sk-ds"
    assert mapped["OPENAI_API_KEY"] == "sk-ds"
    assert mapped["OPENAI_BASE_URL"] == DEEPSEEK_OPENAI_BASE_URL
    assert "ANTHROPIC_API_KEY" not in mapped


def test_deepseek_maps_to_anthropic_compat_for_claude() -> None:
    mapped = map_provider_credentials(
        agent_type="cli.claude",
        provider="deepseek",
        sources={"DEEPSEEK_API_KEY": "sk-ds"},
    )
    assert mapped["ANTHROPIC_API_KEY"] == "sk-ds"
    assert mapped["ANTHROPIC_BASE_URL"] == DEEPSEEK_ANTHROPIC_BASE_URL
    assert mapped["DEEPSEEK_API_KEY"] == "sk-ds"
    assert "OPENAI_API_KEY" not in mapped


def test_custom_without_api_key() -> None:
    mapped = map_provider_credentials(
        agent_type="byo.langgraph",
        provider="custom",
        sources={"NIKA_CUSTOM_BASE_URL": "http://localhost:11434/v1"},
    )
    assert mapped["NIKA_CUSTOM_BASE_URL"] == "http://localhost:11434/v1"
    assert "NIKA_CUSTOM_API_KEY" not in mapped
    assert "OPENAI_API_KEY" not in mapped


def test_custom_openai_compat_maps_key() -> None:
    mapped = map_provider_credentials(
        agent_type="byo.mcp_agent",
        provider="custom",
        sources={
            "NIKA_CUSTOM_BASE_URL": "https://openrouter.ai/api/v1",
            "NIKA_CUSTOM_API_KEY": "sk-or",
        },
    )
    assert mapped["OPENAI_API_KEY"] == "sk-or"
    assert mapped["OPENAI_BASE_URL"] == "https://openrouter.ai/api/v1"


def test_subprocess_env_excludes_forbidden_and_unused_keys() -> None:
    host = {
        "PATH": "/usr/bin",
        "HOME": "/tmp",
        "OPENAI_API_KEY": "sk-openai",
        "DEEPSEEK_API_KEY": "sk-ds",
        "ANTHROPIC_API_KEY": "sk-ant",
        "LANGFUSE_SECRET_KEY": "lf-secret",
        "NIKA_REMOTE_TOKEN": "remote-tok",
        "NIKA_SESSION_ID": "sess-1",
    }
    env = build_agent_subprocess_env(
        agent_type="cli.claude",
        provider="deepseek",
        base=host,
    )
    assert env["ANTHROPIC_API_KEY"] == "sk-ds"
    assert env["DEEPSEEK_API_KEY"] == "sk-ds"
    assert "OPENAI_API_KEY" not in env
    assert "LANGFUSE_SECRET_KEY" not in env
    assert "NIKA_REMOTE_TOKEN" not in env
    assert env["NIKA_SESSION_ID"] == "sess-1"


def test_deprecated_custom_api_base_warns() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        url = resolve_custom_base_url({"CUSTOM_API_BASE": "http://old/v1"})
    assert url == "http://old/v1"
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)


def test_provider_env_context_restores_os_environ() -> None:
    with patch.dict(
        os.environ,
        {
            "OPENAI_API_KEY": "sk-openai",
            "DEEPSEEK_API_KEY": "sk-ds",
            "LANGFUSE_SECRET_KEY": "lf",
            "NIKA_LLM_PROVIDER": "openai",
        },
        clear=False,
    ):
        before_openai = os.environ["OPENAI_API_KEY"]
        before_provider = os.environ["NIKA_LLM_PROVIDER"]
        with provider_env_context(agent_type="byo.autogen", provider="deepseek"):
            assert os.environ["OPENAI_API_KEY"] == "sk-ds"
            assert os.environ["OPENAI_BASE_URL"] == DEEPSEEK_OPENAI_BASE_URL
            assert os.environ["NIKA_LLM_PROVIDER"] == "deepseek"
        assert os.environ["OPENAI_API_KEY"] == before_openai
        assert os.environ["NIKA_LLM_PROVIDER"] == before_provider


def test_provider_env_context_sets_anthropic_for_byo() -> None:
    with patch.dict(
        os.environ,
        {
            "ANTHROPIC_API_KEY": "sk-ant",
            "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
            "OPENAI_API_KEY": "sk-openai",
        },
        clear=False,
    ):
        with provider_env_context(agent_type="byo.mcp_agent", provider="anthropic"):
            assert os.environ["NIKA_LLM_PROVIDER"] == "anthropic"
            assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant"
            assert (
                os.environ["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
            )
            assert "OPENAI_API_KEY" not in os.environ
