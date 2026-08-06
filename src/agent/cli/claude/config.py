"""Claude Code CLI configuration: model defaults and authentication.

NIKA drives ``claude -p`` as a subprocess.  Authentication supports:

1. **Environment API key** — ``ANTHROPIC_API_KEY`` (native Anthropic).
2. **DeepSeek** — user sets ``DEEPSEEK_API_KEY`` + ``NIKA_LLM_PROVIDER=deepseek``;
   NIKA maps it to Anthropic-compatible env for the subprocess only.
3. **Custom OpenAI-/Anthropic-compatible proxy** — ``NIKA_CUSTOM_*``.
4. **Claude subscription / OAuth** — authenticate with ``/login`` so the host
   ``anthropic`` sbx secret is stored (never copy ``~/.claude`` into the sandbox).

Model selection when ``-m`` / ``--model`` is not passed:

``NIKA_CLAUDE_MODEL`` → ``ANTHROPIC_MODEL`` → ``CLAUDE_CODE_SUBAGENT_MODEL`` →
``ANTHROPIC_DEFAULT_SONNET_MODEL``. If none are set, pass ``-m/--model``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import warnings
from typing import Any

from nika.utils.provider_env import (
    ENV_ANTHROPIC_API_KEY,
    ENV_ANTHROPIC_AUTH_TOKEN,
    ENV_ANTHROPIC_BASE_URL,
    ENV_DEEPSEEK_API_KEY,
    build_agent_subprocess_env,
    has_provider_credentials,
    map_provider_credentials,
)

_CLAUDE_MODEL_ENV_KEYS = (
    "NIKA_CLAUDE_MODEL",
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
)


def default_claude_model() -> str:
    """Return the Claude model id from environment variables."""
    for key in _CLAUDE_MODEL_ENV_KEYS:
        if value := os.environ.get(key, "").strip():
            return value
    raise ValueError(
        "Missing Claude model: set NIKA_CLAUDE_MODEL (or ANTHROPIC_MODEL / "
        "CLAUDE_CODE_SUBAGENT_MODEL / ANTHROPIC_DEFAULT_SONNET_MODEL) in .env "
        "or pass -m/--model."
    )


def resolve_claude_model(model: str | None) -> str:
    """Use *model* when set; otherwise fall back to :func:`default_claude_model`."""
    if model and model.strip():
        return model.strip()
    return default_claude_model()


def _resolve_provider(provider: str | None = None) -> str:
    if provider and provider.strip():
        return provider.strip().lower()
    return (os.environ.get("NIKA_LLM_PROVIDER") or "").strip().lower() or "anthropic"


def has_env_claude_credentials(*, provider: str | None = None) -> bool:
    """True when API credentials are supplied via environment variables."""
    prov = _resolve_provider(provider)
    if prov in ("anthropic", "deepseek", "custom"):
        if has_provider_credentials(prov):
            return True
    # Compat: legacy manual DeepSeek Anthropic mapping still counts
    return bool(
        os.environ.get(ENV_ANTHROPIC_API_KEY, "").strip()
        or os.environ.get(ENV_ANTHROPIC_AUTH_TOKEN, "").strip()
        or os.environ.get(ENV_DEEPSEEK_API_KEY, "").strip()
    )


def claude_cli_logged_in(*, timeout_s: float = 10.0) -> bool:
    """True when ``claude auth status`` reports an active login session."""
    if shutil.which("claude") is None:
        return False
    try:
        proc = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        if proc.returncode != 0:
            return False
        data = json.loads(proc.stdout)
        return bool(data.get("loggedIn"))
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        return False


def claude_sbx_secret_available() -> bool:
    """True when the host sbx secret store has an ``anthropic`` entry."""
    from agent.sandbox.sbx.credentials import sbx_anthropic_credential_available

    return sbx_anthropic_credential_available()


def claude_subscription_mode() -> bool:
    """True when Claude is authenticated via sbx secret without env API keys."""
    from agent.sandbox.sbx.credentials import anthropic_subscription_mode

    return anthropic_subscription_mode()


def claude_credentials_available(*, check_cli_login: bool = True) -> bool:
    """True when Claude credentials are configured for host or sandbox use."""
    if has_env_claude_credentials() or claude_sbx_secret_available():
        return True
    if shutil.which("claude") is None:
        return False
    if not check_cli_login:
        return False
    return claude_cli_logged_in()


def use_bare_claude_mode(*, provider: str | None = None) -> bool:
    """Whether to pass ``--bare`` to the Claude subprocess.

    Bare mode isolates the run to environment-based API auth and skips
    keychain / OAuth reads.  Use it for env API-key mode only — never for
    Claude subscription / OAuth (``/login``) which needs non-bare mode.
    """
    if claude_subscription_mode():
        return False
    return has_env_claude_credentials(provider=provider)


def prepare_claude_subprocess_env(
    base: dict[str, str] | None = None,
    *,
    provider: str | None = None,
    agent_type: str = "cli.claude",
) -> dict[str, str]:
    """Build the subprocess environment for ``claude -p``.

    Uses provider-aware credential mapping and does **not** forward the full
    host environment (judge / Langfuse / remote / unused provider keys stay out).
    """
    prov = _resolve_provider(provider)
    host = dict(base if base is not None else os.environ)

    # Compat: if user still has manual ANTHROPIC_AUTH_TOKEN + BASE_URL without
    # NIKA_LLM_PROVIDER=deepseek, treat as anthropic-compat with a warning.
    if (
        prov == "anthropic"
        and not host.get(ENV_ANTHROPIC_API_KEY, "").strip()
        and host.get(ENV_ANTHROPIC_AUTH_TOKEN, "").strip()
    ):
        warnings.warn(
            "ANTHROPIC_AUTH_TOKEN is deprecated for user config; prefer "
            "ANTHROPIC_API_KEY (native) or DEEPSEEK_API_KEY with "
            "NIKA_LLM_PROVIDER=deepseek.",
            DeprecationWarning,
            stacklevel=2,
        )

    if prov in ("anthropic", "deepseek", "custom"):
        return build_agent_subprocess_env(
            agent_type=agent_type, provider=prov, base=host
        )

    # Fallback: map whatever Anthropic/DeepSeek keys exist (legacy)
    env = build_agent_subprocess_env(
        agent_type=agent_type, provider="anthropic", base=host
    )
    mapped = map_provider_credentials(
        agent_type=agent_type, provider="deepseek", sources=host
    )
    if mapped.get(ENV_ANTHROPIC_API_KEY) and not env.get(ENV_ANTHROPIC_API_KEY):
        env.update(mapped)
    return env


def describe_claude_auth(*, provider: str | None = None) -> dict[str, Any]:
    """Summarize detected auth mode (for logging and documentation)."""
    prov = _resolve_provider(provider)
    if has_env_claude_credentials(provider=prov):
        mode = "env_api_key"
        if prov == "deepseek":
            mode = "deepseek"
        elif prov == "custom":
            mode = "custom"
        elif (
            os.environ.get(ENV_ANTHROPIC_AUTH_TOKEN, "").strip()
            and not os.environ.get(ENV_ANTHROPIC_API_KEY, "").strip()
        ):
            mode = "env_token"
        return {
            "mode": mode,
            "provider": prov,
            "bare": use_bare_claude_mode(provider=prov),
            "base_url": os.environ.get(ENV_ANTHROPIC_BASE_URL, "").strip() or None,
            "model_default": default_claude_model(),
        }
    if claude_subscription_mode():
        return {
            "mode": "claude_subscription",
            "provider": prov,
            "bare": False,
            "base_url": None,
            "model_default": default_claude_model(),
        }
    if claude_cli_logged_in():
        return {
            "mode": "claude_login",
            "provider": prov,
            "bare": False,
            "base_url": None,
            "model_default": default_claude_model(),
        }
    return {
        "mode": "none",
        "provider": prov,
        "bare": False,
        "base_url": None,
        "model_default": None,
    }
