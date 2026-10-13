"""Provider and model prerequisites shared by live agent E2E workflows."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.agent.sandbox_support import sandbox_provider_credential_available
from tests.support import integration_pipeline

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("provider", "credentials", "secret_listing", "available"),
    [
        ("deepseek", {"ANTHROPIC_API_KEY": "test-key"}, "", False),
        ("deepseek", {"NIKA_CUSTOM_API_KEY": "test-key"}, "", False),
        ("deepseek", {}, "(global) service anthropic ***", False),
        ("deepseek", {"DEEPSEEK_API_KEY": "   "}, "", False),
        ("deepseek", {"DEEPSEEK_API_KEY": "test-key"}, "", True),
        ("openai", {"DEEPSEEK_API_KEY": "test-key"}, "", False),
        ("openai", {"OPENAI_API_KEY": "test-key"}, "", True),
        ("openai", {}, "(global) service openai ***", True),
        ("openai", {}, "other-sandbox service openai ***", False),
        ("anthropic", {"NIKA_CUSTOM_API_KEY": "test-key"}, "", False),
        ("anthropic", {"ANTHROPIC_AUTH_TOKEN": "test-token"}, "", True),
        ("anthropic", {}, "(global) service anthropic ***", True),
    ],
)
def test_live_credentials_match_provider_and_global_scope(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    credentials: dict[str, str],
    secret_listing: str,
    available: bool,
) -> None:
    for key in (
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "NIKA_CUSTOM_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in credentials.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("tests.agent.sandbox_support.sbx_available", lambda: True)
    monkeypatch.setattr(
        "agent.sandbox.sbx.client.run_sbx_optional",
        lambda _args: SimpleNamespace(returncode=0, stdout=secret_listing, stderr=""),
    )
    assert sandbox_provider_credential_available(provider) is available


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
    with pytest.raises(ValueError, match="must be deepseek or anthropic"):
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
