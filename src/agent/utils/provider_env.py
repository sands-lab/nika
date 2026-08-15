"""Map model-provider credentials into agent process environments.

Maps provider credentials into the env shape each agent/subprocess expects.
Mappings apply only to the returned dict (or a temporary ``os.environ`` patch
for in-process BYO agents). The context manager restores the process environment.
"""

from __future__ import annotations

import logging
import os
import warnings
from contextlib import contextmanager
from typing import Iterator, Mapping

logger = logging.getLogger(__name__)

# Standard provider credentials (user-facing, once each)
ENV_OPENAI_API_KEY = "OPENAI_API_KEY"
ENV_ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
ENV_DEEPSEEK_API_KEY = "DEEPSEEK_API_KEY"

# Custom / self-hosted OpenAI-compatible
ENV_CUSTOM_BASE_URL = "NIKA_CUSTOM_BASE_URL"
ENV_CUSTOM_API_KEY = "NIKA_CUSTOM_API_KEY"
ENV_CUSTOM_MODEL = "NIKA_CUSTOM_MODEL"

# Deprecated aliases (still read with a warning)
_DEPRECATED_CUSTOM_BASE = "CUSTOM_API_BASE"
_DEPRECATED_CUSTOM_KEY = "CUSTOM_API_KEY"

# Claude-compatible runtime keys (mapped, not user-facing primary config)
ENV_ANTHROPIC_AUTH_TOKEN = "ANTHROPIC_AUTH_TOKEN"
ENV_ANTHROPIC_BASE_URL = "ANTHROPIC_BASE_URL"
ENV_OPENAI_BASE_URL = "OPENAI_BASE_URL"

DEEPSEEK_OPENAI_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"

SUPPORTED_PROVIDERS = ("openai", "anthropic", "deepseek", "custom")

# Which providers each agent accepts
AGENT_PROVIDERS: dict[str, frozenset[str]] = {
    "byo.langgraph": frozenset({"openai", "anthropic", "deepseek", "custom"}),
    "byo.mcp_agent": frozenset({"openai", "anthropic", "deepseek", "custom"}),
    "byo.autogen": frozenset({"openai", "anthropic", "deepseek", "custom"}),
    "cli.codex": frozenset({"openai", "deepseek", "custom"}),
    "sdk.codex_sdk": frozenset({"openai", "deepseek", "custom"}),
    "cli.claude": frozenset({"anthropic", "deepseek", "custom"}),
    "sdk.claude_sdk": frozenset({"anthropic", "deepseek", "custom"}),
    "community.sade": frozenset({"anthropic", "deepseek", "custom"}),
    "mock": frozenset(SUPPORTED_PROVIDERS),
}

_CLAUDE_FAMILY = frozenset({"cli.claude", "sdk.claude_sdk", "community.sade"})
_OPENAI_FAMILY = frozenset(
    {
        "byo.langgraph",
        "byo.mcp_agent",
        "byo.autogen",
        "cli.codex",
        "sdk.codex_sdk",
    }
)

# Keys that must never be forwarded into agent subprocess / sandbox runtime
_FORBIDDEN_AGENT_KEYS = frozenset(
    {
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "NIKA_REMOTE_TOKEN",
        "NIKA_JUDGE_PROVIDER",
        "NIKA_JUDGE_MODEL",
    }
)

# Safe OS / PATH scaffolding kept for CLI subprocesses
_BASE_SUBPROCESS_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TMP",
        "TEMP",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "SSH_AUTH_SOCK",
        "DISPLAY",
        "COLORTERM",
        "TERM_PROGRAM",
        # NIKA runtime (non-secret)
        "NIKA_SESSION_ID",
        "NIKA_SESSION_DIR",
        "NIKA_AGENT_TYPE",
        "NIKA_MODEL",
        "NIKA_SANDBOX_EXECUTION",
        "NIKA_SESSION_BACKEND",
        "NIKA_MCP_GATEWAY_AGENT_URL",
        "NIKA_MCP_GATEWAY_URL",
        "NIKA_ENABLE_SKILLS",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "UV_PROJECT_ENVIRONMENT",
        # Codex / Claude local paths
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        # Sandbox markers
        "NIKA_SBX_SANDBOX_NAME",
    }
)

_warned: set[str] = set()


def _env_get(key: str, sources: Mapping[str, str] | None = None) -> str:
    if sources is not None:
        return (sources.get(key) or "").strip()
    return os.environ.get(key, "").strip()


def _warn_deprecated(old: str, new: str) -> None:
    if old in _warned:
        return
    _warned.add(old)
    warnings.warn(
        f"{old} is deprecated; use {new} instead.",
        DeprecationWarning,
        stacklevel=3,
    )
    logger.warning("%s is deprecated; use %s instead.", old, new)


def resolve_custom_base_url(sources: Mapping[str, str] | None = None) -> str:
    """Return custom OpenAI-compatible base URL (new name, then deprecated)."""
    if value := _env_get(ENV_CUSTOM_BASE_URL, sources):
        return value
    if value := _env_get(_DEPRECATED_CUSTOM_BASE, sources):
        _warn_deprecated(_DEPRECATED_CUSTOM_BASE, ENV_CUSTOM_BASE_URL)
        return value
    return ""


def resolve_custom_api_key(sources: Mapping[str, str] | None = None) -> str:
    """Return custom API key if set (optional for unauthenticated endpoints)."""
    if value := _env_get(ENV_CUSTOM_API_KEY, sources):
        return value
    if value := _env_get(_DEPRECATED_CUSTOM_KEY, sources):
        _warn_deprecated(_DEPRECATED_CUSTOM_KEY, ENV_CUSTOM_API_KEY)
        return value
    return ""


def resolve_custom_model(sources: Mapping[str, str] | None = None) -> str | None:
    value = _env_get(ENV_CUSTOM_MODEL, sources)
    return value or None


def validate_provider_for_agent(agent_type: str, provider: str) -> str:
    """Normalize and validate *provider* for *agent_type*."""
    normalized_agent = agent_type.lower()
    normalized = provider.strip().lower()
    if normalized not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported LLM provider {provider!r}. "
            f"Choose one of: {', '.join(SUPPORTED_PROVIDERS)}."
        )
    allowed = AGENT_PROVIDERS.get(normalized_agent)
    if allowed is not None and normalized not in allowed:
        raise ValueError(
            f"Provider {normalized!r} is not supported for agent {agent_type!r}. "
            f"Allowed: {', '.join(sorted(allowed))}."
        )
    return normalized


def provider_credential_keys(provider: str) -> frozenset[str]:
    """Keys that belong to *provider* (for allowlisting / sandbox sync)."""
    match provider:
        case "openai":
            return frozenset({ENV_OPENAI_API_KEY})
        case "anthropic":
            return frozenset({ENV_ANTHROPIC_API_KEY})
        case "deepseek":
            return frozenset({ENV_DEEPSEEK_API_KEY})
        case "custom":
            return frozenset(
                {ENV_CUSTOM_BASE_URL, ENV_CUSTOM_API_KEY, ENV_CUSTOM_MODEL}
            )
        case _:
            return frozenset()


def has_provider_credentials(
    provider: str, sources: Mapping[str, str] | None = None
) -> bool:
    """True when the minimum credential for *provider* is present."""
    match provider:
        case "openai":
            return bool(_env_get(ENV_OPENAI_API_KEY, sources))
        case "anthropic":
            return bool(_env_get(ENV_ANTHROPIC_API_KEY, sources)) or bool(
                _env_get(ENV_ANTHROPIC_AUTH_TOKEN, sources)
            )
        case "deepseek":
            return bool(_env_get(ENV_DEEPSEEK_API_KEY, sources))
        case "custom":
            return bool(resolve_custom_base_url(sources))
        case _:
            return False


def _compat_anthropic_token(sources: Mapping[str, str] | None = None) -> str:
    """Prefer ANTHROPIC_API_KEY; fall back to deprecated AUTH_TOKEN."""
    if value := _env_get(ENV_ANTHROPIC_API_KEY, sources):
        return value
    if value := _env_get(ENV_ANTHROPIC_AUTH_TOKEN, sources):
        _warn_deprecated(ENV_ANTHROPIC_AUTH_TOKEN, ENV_ANTHROPIC_API_KEY)
        return value
    return ""


def map_provider_credentials(
    *,
    agent_type: str,
    provider: str,
    sources: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build credential env vars for *agent_type* + *provider*.

    Only includes keys the agent needs. Does not copy unrelated provider keys.
    """
    provider = validate_provider_for_agent(agent_type, provider)
    agent = agent_type.lower()
    out: dict[str, str] = {}

    if provider == "openai":
        key = _env_get(ENV_OPENAI_API_KEY, sources)
        if key:
            out[ENV_OPENAI_API_KEY] = key
        # agent.custom.base_url can redirect the OpenAI provider to a compatible gateway.
        base = _env_get(ENV_OPENAI_BASE_URL, sources) or resolve_custom_base_url(
            sources
        )
        if base:
            out[ENV_OPENAI_BASE_URL] = base
        return out

    if provider == "anthropic":
        key = _compat_anthropic_token(sources)
        if key:
            out[ENV_ANTHROPIC_API_KEY] = key
        # Official Anthropic needs no base URL. agent.custom.base_url selects a gateway.
        base = _env_get(ENV_ANTHROPIC_BASE_URL, sources) or resolve_custom_base_url(
            sources
        )
        if base:
            out[ENV_ANTHROPIC_BASE_URL] = base
        return out

    if provider == "deepseek":
        key = _env_get(ENV_DEEPSEEK_API_KEY, sources)
        if not key:
            return out
        out[ENV_DEEPSEEK_API_KEY] = key
        if agent in _CLAUDE_FAMILY:
            out[ENV_ANTHROPIC_API_KEY] = key
            out[ENV_ANTHROPIC_AUTH_TOKEN] = key
            out[ENV_ANTHROPIC_BASE_URL] = DEEPSEEK_ANTHROPIC_BASE_URL
        else:
            # OpenAI-compatible agents (langgraph uses ChatDeepSeek separately;
            # mcp/autogen/codex expect OPENAI_*).
            out[ENV_OPENAI_API_KEY] = key
            out[ENV_OPENAI_BASE_URL] = DEEPSEEK_OPENAI_BASE_URL
        return out

    # custom
    base = resolve_custom_base_url(sources)
    if base:
        out[ENV_CUSTOM_BASE_URL] = base
        # Keep deprecated names for libraries that still read them
        out[_DEPRECATED_CUSTOM_BASE] = base
    key = resolve_custom_api_key(sources)
    if key:
        out[ENV_CUSTOM_API_KEY] = key
        out[_DEPRECATED_CUSTOM_KEY] = key
    model = resolve_custom_model(sources)
    if model:
        out[ENV_CUSTOM_MODEL] = model

    if agent in _CLAUDE_FAMILY:
        if key:
            out[ENV_ANTHROPIC_API_KEY] = key
            out[ENV_ANTHROPIC_AUTH_TOKEN] = key
        if base:
            out[ENV_ANTHROPIC_BASE_URL] = base
    elif agent in _OPENAI_FAMILY or agent == "mock":
        if key:
            out[ENV_OPENAI_API_KEY] = key
        if base:
            out[ENV_OPENAI_BASE_URL] = base
    return out


def _copy_base_env(base: Mapping[str, str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in base.items():
        if key in _FORBIDDEN_AGENT_KEYS:
            continue
        if key in _BASE_SUBPROCESS_KEYS or key.startswith("NIKA_"):
            # Still strip remote token / judge even if NIKA_*
            if key in _FORBIDDEN_AGENT_KEYS:
                continue
            if "TOKEN" in key and key != "ANTHROPIC_AUTH_TOKEN":
                if key == "NIKA_REMOTE_TOKEN":
                    continue
            text = str(value).strip() if value is not None else ""
            if text:
                env[key] = str(value)
    # Always allow PATH scaffolding even when empty-ish
    for key in ("PATH", "HOME", "USER", "LANG", "LC_ALL"):
        if key in base and key not in env:
            env[key] = str(base[key])
    return env


def build_agent_subprocess_env(
    *,
    agent_type: str,
    provider: str,
    base: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Minimal env for an agent CLI subprocess (provider-mapped credentials only)."""
    host = dict(base if base is not None else os.environ)
    env = _copy_base_env(host)
    # Drop other provider secrets from the host copy
    for secret in (
        ENV_OPENAI_API_KEY,
        ENV_ANTHROPIC_API_KEY,
        ENV_ANTHROPIC_AUTH_TOKEN,
        ENV_DEEPSEEK_API_KEY,
        ENV_CUSTOM_API_KEY,
        _DEPRECATED_CUSTOM_KEY,
        "LANGFUSE_SECRET_KEY",
        "NIKA_REMOTE_TOKEN",
    ):
        env.pop(secret, None)

    mapped = map_provider_credentials(
        agent_type=agent_type, provider=provider, sources=host
    )
    env.update(mapped)
    if extra:
        env.update({k: v for k, v in extra.items() if v is not None and str(v).strip()})

    # Final scrub of forbidden keys
    for key in list(env):
        if key in _FORBIDDEN_AGENT_KEYS:
            env.pop(key, None)
    return env


@contextmanager
def provider_env_context(
    *,
    agent_type: str,
    provider: str,
) -> Iterator[dict[str, str]]:
    """Temporarily apply mapped provider credentials in ``os.environ``.

    Used for in-process BYO agents. Restores previous values on exit.
    """
    mapped = map_provider_credentials(agent_type=agent_type, provider=provider)
    # Also clear unused provider secrets for the duration
    clear_keys = [
        ENV_OPENAI_API_KEY,
        ENV_ANTHROPIC_API_KEY,
        ENV_ANTHROPIC_AUTH_TOKEN,
        ENV_ANTHROPIC_BASE_URL,
        ENV_DEEPSEEK_API_KEY,
        ENV_OPENAI_BASE_URL,
        ENV_CUSTOM_API_KEY,
        ENV_CUSTOM_BASE_URL,
        _DEPRECATED_CUSTOM_BASE,
        _DEPRECATED_CUSTOM_KEY,
    ]
    previous: dict[str, str | None] = {key: os.environ.get(key) for key in clear_keys}
    previous.update({key: os.environ.get(key) for key in mapped})

    try:
        for key in clear_keys:
            os.environ.pop(key, None)
        os.environ.update(mapped)
        yield mapped
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
