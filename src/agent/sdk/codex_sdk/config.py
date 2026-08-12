"""Configuration helpers for sdk.codex_sdk."""

from __future__ import annotations

import os
from pathlib import Path

from agent.cli.codex.codex_worker import REASONING_EFFORT_LEVELS
from agent.utils.provider_env import (
    ENV_DEEPSEEK_API_KEY,
    ENV_OPENAI_API_KEY,
    has_provider_credentials,
)


def codex_sdk_local_auth_available() -> bool:
    """True when Codex credentials are available for sandbox or local use.

    Accepts provider credentials from ``NIKA_LLM_PROVIDER`` (openai / deepseek /
    custom), an existing ``openai`` sbx secret (API key or OAuth), or a host
    ``~/.codex/auth.json`` for non-sandbox tooling. Host auth files are never
    copied into Docker Sandboxes.
    """
    provider = (os.environ.get("NIKA_LLM_PROVIDER") or "").strip().lower()
    if provider and has_provider_credentials(provider):
        return True
    if (
        os.environ.get(ENV_DEEPSEEK_API_KEY, "").strip()
        or os.environ.get(ENV_OPENAI_API_KEY, "").strip()
    ):
        return True
    try:
        from agent.sandbox.sbx.credentials import sbx_openai_credential_available

        if sbx_openai_credential_available():
            return True
    except Exception:
        pass
    return (Path.home() / ".codex" / "auth.json").is_file()


def validate_reasoning_effort(reasoning_effort: str | None) -> str | None:
    if reasoning_effort is None:
        return None
    if reasoning_effort not in REASONING_EFFORT_LEVELS:
        raise ValueError(
            f"reasoning_effort must be one of {REASONING_EFFORT_LEVELS}, got {reasoning_effort!r}"
        )
    return reasoning_effort
