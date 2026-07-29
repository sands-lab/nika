"""Credential helpers for sdk.claude_sdk."""

from __future__ import annotations

import os

from agent.cli.claude.config import (
    claude_sbx_secret_available,
    has_env_claude_credentials,
    prepare_claude_subprocess_env,
    resolve_claude_model,
)


def claude_sdk_credentials_available() -> bool:
    """True when Anthropic credentials are configured (env API key or sbx secret)."""
    return has_env_claude_credentials() or claude_sbx_secret_available()


def prepare_claude_sdk_env(*, session_id: str) -> dict[str, str]:
    """Build SDK env with Anthropic credentials and session context."""
    if not claude_sdk_credentials_available():
        raise RuntimeError(
            "Anthropic credentials are not set. Configure ANTHROPIC_API_KEY "
            "or ANTHROPIC_AUTH_TOKEN (+ optional ANTHROPIC_BASE_URL) in the repo-root "
            ".env, or authenticate with Claude `/login` so the host `anthropic` "
            "sbx secret is stored (see docs/agent-sandbox.md)."
        )
    from agent.sandbox.sbx.auth import PROXY_MANAGED_SENTINEL, in_sandbox

    env = prepare_claude_subprocess_env()
    env["NIKA_SESSION_ID"] = session_id
    if in_sandbox():
        # Inside the microVM only placeholders/sentinels are valid. Never remap
        # host DeepSeek/Anthropic secrets (they are not present) over a
        # set-custom placeholder already applied from runtime env.
        api_key = (
            os.environ.get("ANTHROPIC_API_KEY", "").strip() or PROXY_MANAGED_SENTINEL
        )
        env["ANTHROPIC_API_KEY"] = api_key
        base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
        if base:
            env["ANTHROPIC_BASE_URL"] = base
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.pop("DEEPSEEK_API_KEY", None)
    return env


def resolve_claude_sdk_model(model: str | None) -> str:
    """Use *model* when set; otherwise fall back to the Claude CLI model chain."""
    return resolve_claude_model(model)
