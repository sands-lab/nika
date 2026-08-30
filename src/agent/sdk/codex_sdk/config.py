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


def codex_sdk_local_auth_available(provider: str | None = None) -> bool:
    """True when Codex credentials are available for sandbox or local use.

    When *provider* is set, check that provider's credentials. Without a
    provider, any OpenAI or DeepSeek API key, an ``openai`` sbx secret, or a host
    ``~/.codex/auth.json`` still counts. Host auth files are never copied into
    Docker Sandboxes.
    """
    if provider and str(provider).strip():
        if has_provider_credentials(str(provider).strip().lower()):
            return True
    elif (
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
