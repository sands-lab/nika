"""Claude Code CLI configuration: model defaults and authentication.

NIKA drives ``claude -p`` as a subprocess.  Authentication supports:

1. **Environment API key** — ``ANTHROPIC_API_KEY`` (native Anthropic or any
   provider that accepts the standard header). Synced into ``sbx secret`` for
   sandbox runs; the microVM only sees a proxy-managed sentinel.
2. **Environment token + base URL** — ``ANTHROPIC_AUTH_TOKEN`` with optional
   ``ANTHROPIC_BASE_URL`` (e.g. DeepSeek's Anthropic-compatible endpoint).
3. **Claude subscription / OAuth** — authenticate with ``/login`` so the host
   ``anthropic`` sbx secret is stored (never copy ``~/.claude`` into the sandbox).
   Subprocess runs without ``--bare`` so OAuth can be used.

Model selection reads from env when ``-m`` / ``--model`` is not passed:

``ANTHROPIC_MODEL`` → ``CLAUDE_CODE_SUBAGENT_MODEL`` →
``ANTHROPIC_DEFAULT_SONNET_MODEL``. If none are set, pass ``-m/--model`` or configure ``.env``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

_CLAUDE_MODEL_ENV_KEYS = (
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
        "Missing Claude model: set ANTHROPIC_MODEL (or CLAUDE_CODE_SUBAGENT_MODEL / "
        "ANTHROPIC_DEFAULT_SONNET_MODEL) in .env or pass -m/--model."
    )


def resolve_claude_model(model: str | None) -> str:
    """Use *model* when set; otherwise fall back to :func:`default_claude_model`."""
    if model and model.strip():
        return model.strip()
    return default_claude_model()


def has_env_claude_credentials() -> bool:
    """True when API credentials are supplied via environment variables."""
    return bool(
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
        or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        or os.environ.get("DEEPSEEK_API_KEY", "").strip()
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


def use_bare_claude_mode() -> bool:
    """Whether to pass ``--bare`` to the Claude subprocess.

    Bare mode isolates the run to environment-based API auth and skips
    keychain / OAuth reads.  Use it for env API-key mode only — never for
    Claude subscription / OAuth (``/login``) which needs non-bare mode.
    """
    if claude_subscription_mode():
        return False
    return has_env_claude_credentials()


def prepare_claude_subprocess_env(
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the subprocess environment for ``claude -p``.

    * Copies *base* or ``os.environ``.
    * Maps ``ANTHROPIC_AUTH_TOKEN`` / ``DEEPSEEK_API_KEY`` → ``ANTHROPIC_API_KEY``
      when the latter is unset.
    * Defaults ``ANTHROPIC_BASE_URL`` to DeepSeek Anthropic-compatible endpoint
      when using DeepSeek keys without an explicit base URL.
    """
    env = dict(base if base is not None else os.environ)
    if not env.get("ANTHROPIC_API_KEY", "").strip():
        for key in ("ANTHROPIC_AUTH_TOKEN", "DEEPSEEK_API_KEY"):
            if env.get(key, "").strip():
                env["ANTHROPIC_API_KEY"] = env[key]
                break
    if not env.get("ANTHROPIC_BASE_URL", "").strip() and (
        env.get("DEEPSEEK_API_KEY", "").strip()
        or env.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    ):
        env["ANTHROPIC_BASE_URL"] = "https://api.deepseek.com/anthropic"
    return env


def describe_claude_auth() -> dict[str, Any]:
    """Summarize detected auth mode (for logging and documentation)."""
    if has_env_claude_credentials():
        mode = (
            "env_token"
            if os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
            else "env_api_key"
        )
        return {
            "mode": mode,
            "bare": use_bare_claude_mode(),
            "base_url": os.environ.get("ANTHROPIC_BASE_URL", "").strip() or None,
            "model_default": default_claude_model(),
        }
    if claude_subscription_mode():
        return {
            "mode": "claude_subscription",
            "bare": False,
            "base_url": None,
            "model_default": default_claude_model(),
        }
    if claude_cli_logged_in():
        return {
            "mode": "claude_login",
            "bare": False,
            "base_url": None,
            "model_default": default_claude_model(),
        }
    return {
        "mode": "none",
        "bare": False,
        "base_url": None,
        "model_default": None,
    }
