"""Claude Code CLI configuration: model defaults and authentication.

NIKA drives ``claude -p`` as a subprocess. Authentication supports:

1. **Environment API key**: ``ANTHROPIC_API_KEY`` for native Anthropic.
2. **DeepSeek**: the user sets ``DEEPSEEK_API_KEY`` and ``agent.provider: deepseek``
   (config/nika.yaml or ``-p``); NIKA maps it to Anthropic-compatible env for
   the subprocess only.
3. **Custom Anthropic-compatible proxy**: ``NIKA_CUSTOM_*`` with
   ``agent.provider: custom``.
4. **Claude subscription or OAuth**: authenticate with ``/login`` so the host
   ``anthropic`` sbx secret is stored (never copy ``~/.claude`` into the sandbox).

Model selection: pass ``-m`` / ``--model``, or set ``agent.model`` in config/nika.yaml.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import warnings
from typing import Any

from agent.utils.provider_env import (
    ENV_ANTHROPIC_API_KEY,
    ENV_ANTHROPIC_AUTH_TOKEN,
    ENV_ANTHROPIC_BASE_URL,
    ENV_DEEPSEEK_API_KEY,
    build_agent_subprocess_env,
    has_provider_credentials,
    map_provider_credentials,
)

# Anthropic CLI and sandbox compatibility. Host model resolution does not read these.
_CLAUDE_MODEL_ENV_KEYS = (
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
)


def default_claude_model() -> str:
    """Return the model advertised by the Anthropic CLI environment.

    Host model resolution uses ``agent.model`` or ``-m`` through
    :func:`resolve_claude_model`.
    """
    for key in _CLAUDE_MODEL_ENV_KEYS:
        if value := os.environ.get(key, "").strip():
            return value
    raise ValueError(
        "Missing Claude model: set agent.model in config/nika.yaml "
        "or pass -m/--model."
    )


def resolve_claude_model(model: str | None) -> str:
    """Use *model* when set; otherwise raise (YAML / ``-m`` required)."""
    if model and model.strip():
        return model.strip()
    raise ValueError(
        "Missing Claude model: set agent.model in config/nika.yaml "
        "or pass -m/--model."
    )


def _resolve_provider(provider: str | None) -> str:
    if provider and str(provider).strip():
        return str(provider).strip().lower()
    raise ValueError(
        "Missing LLM provider: set agent.provider in config/nika.yaml "
        "or pass -p/--provider."
    )


def has_env_claude_credentials(*, provider: str | None = None) -> bool:
    """True when API credentials are supplied via environment variables."""
    if provider and str(provider).strip():
        prov = str(provider).strip().lower()
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
    provider: str,
    agent_type: str = "cli.claude",
) -> dict[str, str]:
    """Build the subprocess environment for ``claude -p``.

    Uses provider-aware credential mapping and does **not** forward the full
    host environment (judge / Langfuse / remote / unused provider keys stay out).
    """
    from agent.sandbox.sbx.auth import PROXY_MANAGED_SENTINEL
    from agent.sandbox.sbx.exec import sandbox_name_from_env

    prov = _resolve_provider(provider)
    host = dict(base if base is not None else os.environ)

    def _is_placeholder(value: str) -> bool:
        text = value.strip()
        return text == PROXY_MANAGED_SENTINEL or text.startswith("sbx-cs-")

    # Support a legacy manual token and base URL long enough to emit a migration warning.
    if (
        prov == "anthropic"
        and not host.get(ENV_ANTHROPIC_API_KEY, "").strip()
        and host.get(ENV_ANTHROPIC_AUTH_TOKEN, "").strip()
    ):
        warnings.warn(
            "ANTHROPIC_AUTH_TOKEN is deprecated for user config; prefer "
            "ANTHROPIC_API_KEY (native) or DEEPSEEK_API_KEY with "
            "agent.provider: deepseek (config/nika.yaml or -p).",
            DeprecationWarning,
            stacklevel=2,
        )

    if prov in ("anthropic", "deepseek", "custom"):
        env = build_agent_subprocess_env(
            agent_type=agent_type, provider=prov, base=host
        )
    else:
        # Fallback: map whatever Anthropic/DeepSeek keys exist (legacy)
        env = build_agent_subprocess_env(
            agent_type=agent_type, provider="anthropic", base=host
        )
        mapped = map_provider_credentials(
            agent_type=agent_type, provider="deepseek", sources=host
        )
        if mapped.get(ENV_ANTHROPIC_API_KEY) and not env.get(ENV_ANTHROPIC_API_KEY):
            env.update(mapped)

    # Sandbox sessions already injected sbx-cs-* / proxy-managed placeholders on
    # the host env. Never remap real DEEPSEEK/custom keys over them — sbx exec
    # would then omit the real secrets and leave the microVM without credentials.
    in_sbx_session = bool(sandbox_name_from_env())
    for key in (ENV_ANTHROPIC_API_KEY, ENV_ANTHROPIC_AUTH_TOKEN):
        host_val = host.get(key, "").strip()
        if _is_placeholder(host_val):
            env[key] = host_val
        elif in_sbx_session and _is_placeholder(os.environ.get(key, "").strip()):
            env[key] = os.environ[key].strip()
    if in_sbx_session or any(
        _is_placeholder(host.get(k, ""))
        for k in (ENV_ANTHROPIC_API_KEY, ENV_ANTHROPIC_AUTH_TOKEN)
    ):
        base_url = (
            host.get(ENV_ANTHROPIC_BASE_URL, "").strip()
            or os.environ.get(ENV_ANTHROPIC_BASE_URL, "").strip()
        )
        if base_url:
            env[ENV_ANTHROPIC_BASE_URL] = base_url
        env.pop(ENV_DEEPSEEK_API_KEY, None)
        # Prefer AUTH_TOKEN alias when API_KEY placeholder is present (DeepSeek docs).
        api_key = env.get(ENV_ANTHROPIC_API_KEY, "").strip()
        if (
            _is_placeholder(api_key)
            and not env.get(ENV_ANTHROPIC_AUTH_TOKEN, "").strip()
        ):
            env[ENV_ANTHROPIC_AUTH_TOKEN] = api_key
    return env


def describe_claude_auth(*, provider: str) -> dict[str, Any]:
    """Summarize detected auth mode (for logging and documentation)."""
    prov = _resolve_provider(provider)
    try:
        model_default: str | None = default_claude_model()
    except ValueError:
        model_default = None
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
            "model_default": model_default,
        }
    if claude_subscription_mode():
        return {
            "mode": "claude_subscription",
            "provider": prov,
            "bare": False,
            "base_url": None,
            "model_default": model_default,
        }
    if claude_cli_logged_in():
        return {
            "mode": "claude_login",
            "provider": prov,
            "bare": False,
            "base_url": None,
            "model_default": model_default,
        }
    return {
        "mode": "none",
        "provider": prov,
        "bare": False,
        "base_url": None,
        "model_default": None,
    }
