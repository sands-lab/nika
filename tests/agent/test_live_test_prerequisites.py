"""Provider and model prerequisites shared by live agent E2E workflows."""

from __future__ import annotations

import json

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.agent.sandbox_support import sandbox_provider_credential_available
from tests.support import integration_pipeline

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("provider", "credentials", "available"),
    [
        ("deepseek", {"ANTHROPIC_API_KEY": "test-key"}, False),
        ("deepseek", {"NIKA_CUSTOM_API_KEY": "test-key"}, False),
        ("deepseek", {"DEEPSEEK_API_KEY": "   "}, False),
        ("deepseek", {"DEEPSEEK_API_KEY": "test-key"}, True),
        ("openai", {"DEEPSEEK_API_KEY": "test-key"}, False),
        ("openai", {"OPENAI_API_KEY": "test-key"}, True),
        ("anthropic", {"NIKA_CUSTOM_API_KEY": "test-key"}, False),
        ("anthropic", {"ANTHROPIC_AUTH_TOKEN": "test-token"}, True),
        ("anthropic", {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"}, True),
    ],
)
def test_live_credentials_match_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
    credentials: dict[str, str],
    available: bool,
) -> None:
    for key in (
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "NIKA_CUSTOM_API_KEY",
        "OPENAI_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in credentials.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("nika.agents.sandbox.subscription.CODEX_LOGIN_HOME", tmp_path)
    monkeypatch.setattr(
        "tests.agent.sandbox_support.resolve_sandbox_config",
        lambda: SimpleNamespace(env_file=tmp_path / ".env"),
    )
    assert sandbox_provider_credential_available(provider) is available


def test_live_credentials_use_the_host_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every runtime uses NIKA's host login; the custom provider needs its endpoint."""
    monkeypatch.setattr("nika.agents.sandbox.subscription.CODEX_LOGIN_HOME", tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("NIKA_TEST_CUSTOM_BASE_URL", raising=False)
    for backend in ("sbx", "e2b"):
        assert not sandbox_provider_credential_available("openai", backend=backend)
    tokens = {
        "access_token": "a",
        "refresh_token": "r",
        "id_token": "i",
        "account_id": "x",
    }
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": tokens}))
    for backend in ("sbx", "e2b"):
        assert sandbox_provider_credential_available("openai", backend=backend)
    assert not sandbox_provider_credential_available("custom", backend="sbx")
    monkeypatch.setenv("NIKA_TEST_CUSTOM_BASE_URL", "http://model.example/v1")
    assert sandbox_provider_credential_available("custom", backend="sbx")


def test_live_model_defaults_and_anthropic_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "NIKA_TEST_ANTHROPIC_PROVIDER",
        "NIKA_TEST_ANTHROPIC_MODEL",
        "NIKA_TEST_CODEX_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    assert integration_pipeline.live_anthropic_test_config() == (
        "deepseek",
        "deepseek-v4-flash",
    )
    assert integration_pipeline.live_codex_test_model() == "gpt-5-mini"
    monkeypatch.setenv("NIKA_TEST_ANTHROPIC_PROVIDER", "anthropic")
    with pytest.raises(ValueError, match="NIKA_TEST_ANTHROPIC_MODEL"):
        integration_pipeline.live_anthropic_test_config()
    monkeypatch.setenv("NIKA_TEST_ANTHROPIC_MODEL", "account-model")
    assert integration_pipeline.live_anthropic_test_config() == (
        "anthropic",
        "account-model",
    )
    monkeypatch.setenv("NIKA_TEST_ANTHROPIC_PROVIDER", "custom")
    assert integration_pipeline.live_anthropic_test_config() == (
        "custom",
        "account-model",
    )
    monkeypatch.setenv("NIKA_TEST_ANTHROPIC_PROVIDER", "openai")
    with pytest.raises(ValueError, match="must be deepseek, anthropic, or custom"):
        integration_pipeline.live_anthropic_test_config()


def test_load_test_env_reads_repository_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=dotenv-test-key\n")
    monkeypatch.setattr(
        integration_pipeline,
        "__file__",
        str(tmp_path / "tests/support/integration_pipeline.py"),
    )
    integration_pipeline.load_test_env()
    assert integration_pipeline.deepseek_api_key_available()
