"""SADE credential helpers (aligned with ``cli.claude``)."""

from __future__ import annotations

import os

from agent.cli.claude.config import (
    claude_sbx_secret_available,
    has_env_claude_credentials,
    prepare_claude_subprocess_env,
)


def sade_credentials_available() -> bool:
    """True when Anthropic credentials are configured (env or sbx secret)."""
    return has_env_claude_credentials() or claude_sbx_secret_available()


def prepare_sade_sdk_env(*, session_id: str) -> dict[str, str]:
    """Build SDK env with Anthropic credentials and session context."""
    if not sade_credentials_available():
        raise RuntimeError(
            "Anthropic credentials are not set. Configure ANTHROPIC_API_KEY "
            "or ANTHROPIC_AUTH_TOKEN (+ optional ANTHROPIC_BASE_URL) in the repo-root "
            ".env, or authenticate with Claude `/login` so the host `anthropic` "
            "sbx secret is stored before running `nika agent run -a community.sade`."
        )
    from agent.sandbox.sbx.auth import PROXY_MANAGED_SENTINEL, in_sandbox

    env = prepare_claude_subprocess_env()
    env["NIKA_SESSION_ID"] = session_id
    if in_sandbox():
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
