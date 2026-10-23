"""Unit tests for provider credential mapping."""

from __future__ import annotations

import warnings

import pytest

from nika.agents.provider_env import (
    CUSTOM_UNAUTHENTICATED_API_KEY,
    DEEPSEEK_ANTHROPIC_BASE_URL,
    DEEPSEEK_OPENAI_BASE_URL,
    map_provider_credentials,
    resolve_custom_base_url,
    validate_provider_for_agent,
)

pytestmark = pytest.mark.unit


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


@pytest.mark.parametrize(
    ("agent_type", "provider", "key"),
    [
        ("byo.mcp_agent", "anthropic", "ANTHROPIC_API_KEY"),
        ("byo.langgraph", "openai", "OPENAI_API_KEY"),
    ],
)
def test_builtin_providers_ignore_custom_base_url(
    agent_type: str, provider: str, key: str
) -> None:
    mapped = map_provider_credentials(
        agent_type=agent_type,
        provider=provider,
        sources={key: "sk-test", "NIKA_CUSTOM_BASE_URL": "https://gateway.example/v1"},
    )
    assert not any(name.endswith("_BASE_URL") for name in mapped)


def test_anthropic_official_omits_base_url() -> None:
    mapped = map_provider_credentials(
        agent_type="cli.claude",
        provider="anthropic",
        sources={"ANTHROPIC_API_KEY": "sk-ant"},
    )
    assert mapped["ANTHROPIC_API_KEY"] == "sk-ant"
    assert "ANTHROPIC_BASE_URL" not in mapped
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


def test_openai_maps_gateway_base_url() -> None:
    mapped = map_provider_credentials(
        agent_type="cli.opencode",
        provider="openai",
        sources={
            "OPENAI_API_KEY": "sk-test",
            "OPENAI_BASE_URL": "http://lb.example/v1",
        },
    )
    assert mapped["OPENAI_API_KEY"] == "sk-test"
    assert mapped["OPENAI_BASE_URL"] == "http://lb.example/v1"


def test_deepseek_honors_gateway_base_url() -> None:
    mapped = map_provider_credentials(
        agent_type="cli.dsh",
        provider="deepseek",
        sources={
            "DEEPSEEK_API_KEY": "sk-ds",
            "DEEPSEEK_BASE_URL": "http://lb.example/v1",
        },
    )
    assert mapped["DEEPSEEK_API_KEY"] == "sk-ds"
    assert mapped["OPENAI_API_KEY"] == "sk-ds"
    assert mapped["OPENAI_BASE_URL"] == "http://lb.example/v1"


def test_deepseek_gateway_strips_v1_for_anthropic_family() -> None:
    mapped = map_provider_credentials(
        agent_type="cli.claude",
        provider="deepseek",
        sources={
            "DEEPSEEK_API_KEY": "sk-ds",
            "DEEPSEEK_BASE_URL": "http://gateway.example/v1",
        },
    )
    assert mapped["ANTHROPIC_BASE_URL"] == "http://gateway.example"


def test_process_env_overlays_openai_gateway(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nika.agents.sandbox.config import load_credential_sources

    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "from-proc")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://lb.example/v1")
    sources = load_credential_sources(
        env_file, provider="openai", agent_type="cli.opencode"
    )
    assert sources["OPENAI_API_KEY"] == "from-proc"
    assert sources["OPENAI_BASE_URL"] == "http://lb.example/v1"


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
    assert mapped["OPENAI_API_KEY"] == CUSTOM_UNAUTHENTICATED_API_KEY
    assert mapped["OPENAI_BASE_URL"] == "http://localhost:11434/v1"


def test_custom_without_api_key_maps_for_codex() -> None:
    mapped = map_provider_credentials(
        agent_type="cli.codex",
        provider="custom",
        sources={"NIKA_CUSTOM_BASE_URL": "http://gateway.example:8000/v1"},
    )
    assert mapped["OPENAI_API_KEY"] == CUSTOM_UNAUTHENTICATED_API_KEY
    assert mapped["OPENAI_BASE_URL"] == "http://gateway.example:8000/v1"


def test_custom_without_api_key_maps_for_claude() -> None:
    mapped = map_provider_credentials(
        agent_type="cli.claude",
        provider="custom",
        sources={"NIKA_CUSTOM_BASE_URL": "http://gateway.example/anthropic"},
    )
    assert mapped["ANTHROPIC_API_KEY"] == CUSTOM_UNAUTHENTICATED_API_KEY
    assert mapped["ANTHROPIC_AUTH_TOKEN"] == CUSTOM_UNAUTHENTICATED_API_KEY
    assert mapped["ANTHROPIC_BASE_URL"] == "http://gateway.example/anthropic"


def test_custom_openai_compat_strips_v1_for_claude() -> None:
    mapped = map_provider_credentials(
        agent_type="cli.claude",
        provider="custom",
        sources={"NIKA_CUSTOM_BASE_URL": "http://gateway.example:8000/v1"},
    )
    assert mapped["ANTHROPIC_BASE_URL"] == "http://gateway.example:8000"


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


def test_deprecated_custom_api_base_warns() -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        url = resolve_custom_base_url({"CUSTOM_API_BASE": "http://old/v1"})
    assert url == "http://old/v1"
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)
