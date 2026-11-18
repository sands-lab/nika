"""Subscription logins held on the host for the model proxy."""

from __future__ import annotations

import base64
import json
import stat
import time
from contextlib import ExitStack
from types import SimpleNamespace

import pytest

from nika.agents.sandbox import subscription
from nika.agents.sandbox.subscription import CodexLogin

pytestmark = pytest.mark.unit

_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "DEEPSEEK_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_BASE_URL",
    "ANTHROPIC_BASE_URL",
)


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    return f"header.{payload.rstrip('=')}.signature"


def _write_login(home, *, expires_in: float) -> None:
    home.mkdir(parents=True, exist_ok=True)
    tokens = {
        "access_token": _jwt({"exp": time.time() + expires_in}),
        "refresh_token": "refresh-1",
        "id_token": _jwt({"aud": ["app_test"], "iss": "https://auth.example"}),
        "account_id": "account-1",
    }
    (home / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": tokens})
    )


def test_codex_login_refreshes_an_expired_token_and_writes_it_back(
    tmp_path, monkeypatch
) -> None:
    _write_login(tmp_path, expires_in=-60)
    fresh = _jwt({"exp": time.time() + 3600})
    calls = []

    def post(url, *, json, timeout):
        calls.append((url, json))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"access_token": fresh, "refresh_token": "refresh-2"},
        )

    monkeypatch.setattr(subscription.httpx, "post", post)
    login = CodexLogin.load(tmp_path)

    assert login.access_token() == fresh
    assert login.access_token() == fresh
    # The login's own OAuth client and issuer, refreshed once.
    assert calls == [
        (
            "https://auth.example/oauth/token",
            {
                "client_id": "app_test",
                "grant_type": "refresh_token",
                "refresh_token": "refresh-1",
                "scope": "openid profile email",
            },
        )
    ]
    saved = json.loads((tmp_path / "auth.json").read_text())
    assert saved["tokens"]["access_token"] == fresh
    assert saved["tokens"]["refresh_token"] == "refresh-2"
    assert saved["last_refresh"]
    assert stat.S_IMODE((tmp_path / "auth.json").stat().st_mode) == 0o600


def test_codex_login_keeps_a_valid_token(tmp_path, monkeypatch) -> None:
    _write_login(tmp_path, expires_in=3600)
    monkeypatch.setattr(subscription.httpx, "post", pytest.fail)
    login = CodexLogin.load(tmp_path)
    assert login.account_id == "account-1"
    assert login.access_token()
    assert CodexLogin.load(tmp_path / "missing") is None


def test_model_proxy_uses_a_subscription_only_without_api_key(
    tmp_path, monkeypatch
) -> None:
    from nika.agents.sandbox.model_proxy import start_model_proxy

    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    monkeypatch.setattr(subscription, "CODEX_LOGIN_HOME", tmp_path / "codex")

    def start(agent_type: str, provider: str) -> dict[str, str]:
        with ExitStack() as stack:
            _, env = start_model_proxy(
                stack,
                env_file=env_file,
                agent_type=agent_type,
                provider=provider,
                bind_host="127.0.0.1",
                agent_host="127.0.0.1",
            )
        return env

    env_file.write_text("")
    with pytest.raises(ValueError, match="claude setup-token"):
        start("cli.claude", "anthropic")
    with pytest.raises(ValueError, match="codex login"):
        start("cli.codex", "openai")

    # The sandbox only gets the proxy token, as Claude Code's OAuth bearer.
    env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-real\n")
    env = start("cli.claude", "anthropic")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"].startswith("proxy-token-")
    # Claude Code prefers an API key, so one the runtime injects is cleared.
    assert env["ANTHROPIC_API_KEY"] == env["ANTHROPIC_AUTH_TOKEN"] == ""

    _write_login(tmp_path / "codex", expires_in=3600)
    env = start("cli.codex", "openai")
    assert set(env) == {"OPENAI_BASE_URL", "OPENAI_API_KEY"}
    assert env["OPENAI_API_KEY"].startswith("proxy-token-")

    # BYO agents need an API key even when a login is present.
    for provider in ("openai", "anthropic"):
        with pytest.raises(ValueError, match="needs an API key"):
            start("byo.langgraph", provider)

    # An API key still wins over the subscription.
    env_file.write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-real\nANTHROPIC_API_KEY=sk-ant-key\n"
    )
    env = start("cli.claude", "anthropic")
    assert set(env) == {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    }
    assert "sk-ant" not in json.dumps(env)


def test_agents_use_the_subscription_stand_in_on_e2b(monkeypatch) -> None:
    from agent.cli.claude.config import claude_env, use_bare_claude_mode
    from agent.cli.codex.codex_worker import _build_mcp_toml

    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    proxy = "http://127.0.0.1:41000"
    token = "proxy-token-session"

    # Codex reaches the proxy through its own provider block.
    monkeypatch.setenv("OPENAI_BASE_URL", proxy)
    monkeypatch.setenv("OPENAI_API_KEY", token)
    config = _build_mcp_toml({}, provider="openai")
    assert 'model_provider = "openai_llm"' in config
    assert f'base_url = "{proxy}"' in config
    # Without a base URL (sbx) Codex keeps its built-in provider.
    monkeypatch.delenv("OPENAI_BASE_URL")
    assert "model_provider" not in _build_mcp_toml({}, provider="openai")

    # Claude Code runs in subscription mode with the proxy token.
    monkeypatch.setenv("ANTHROPIC_BASE_URL", proxy)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", token)
    assert not use_bare_claude_mode()
    assert "ANTHROPIC_AUTH_TOKEN" not in claude_env()
