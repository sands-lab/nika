"""Map model-provider credentials onto the client variables agents read.

The host resolves the provider credentials in ``.env`` (and ``agent.base_url``)
into the standard client variables of the agent's client family; sandbox
backends then hand the agent placeholders or a model proxy, never the keys.
"""

from __future__ import annotations

import logging
import os
import warnings
from typing import TYPE_CHECKING, Mapping

# Standard provider credentials and endpoints (shared with agent code).
from agent.llm.endpoints import (
    DEEPSEEK_ANTHROPIC_BASE_URL,
    DEEPSEEK_OPENAI_BASE_URL,
    ENV_ANTHROPIC_API_KEY,
    ENV_ANTHROPIC_AUTH_TOKEN,
    ENV_ANTHROPIC_BASE_URL,
    ENV_DEEPSEEK_API_KEY,
    ENV_OPENAI_API_KEY,
    ENV_OPENAI_BASE_URL,
    SUPPORTED_PROVIDERS,
    UNAUTHENTICATED_API_KEY as CUSTOM_UNAUTHENTICATED_API_KEY,
)

if TYPE_CHECKING:
    from agent.runtime.task import LlmClientSettings

logger = logging.getLogger(__name__)

# Custom / self-hosted OpenAI-compatible
ENV_CUSTOM_BASE_URL = "NIKA_CUSTOM_BASE_URL"
ENV_CUSTOM_API_KEY = "NIKA_CUSTOM_API_KEY"
ENV_CUSTOM_MODEL = "NIKA_CUSTOM_MODEL"

# Deprecated aliases (still read with a warning)
_DEPRECATED_CUSTOM_BASE = "CUSTOM_API_BASE"
_DEPRECATED_CUSTOM_KEY = "CUSTOM_API_KEY"


def agent_providers(agent_type: str) -> frozenset[str] | None:
    """Providers *agent_type* accepts, or ``None`` for an unknown agent."""
    from nika.agents.registry import find_agent_spec

    spec = find_agent_spec(agent_type)
    return spec.providers if spec is not None else None


def _credential_family(agent_type: str, provider: str) -> str:
    from nika.agents.registry import find_agent_spec

    spec = find_agent_spec(agent_type)
    return spec.client_family(provider) if spec is not None else "openai"


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


def openai_compat_to_anthropic_base_url(url: str) -> str:
    """Strip a trailing ``/v1`` so Anthropic clients do not request ``/v1/v1/messages``.

    OpenAI-compatible gateways are usually configured as ``http://host:port/v1``.
    The Anthropic SDK appends ``/v1/messages`` itself, so Claude family agents need
    the host root (or an Anthropic-native path like ``.../anthropic``).
    """
    text = (url or "").strip().rstrip("/")
    if text.endswith("/v1"):
        return text[:-3] or text
    return (url or "").strip()


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
    normalized = provider.strip().lower()
    if normalized not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Unsupported LLM provider {provider!r}. "
            f"Choose one of: {', '.join(SUPPORTED_PROVIDERS)}."
        )
    allowed = agent_providers(agent_type)
    if allowed is not None and normalized not in allowed:
        raise ValueError(
            f"Provider {normalized!r} is not supported for agent {agent_type!r}. "
            f"Allowed: {', '.join(sorted(allowed))}."
        )
    return normalized


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
    family = _credential_family(agent_type, provider)
    out: dict[str, str] = {}

    if provider == "openai":
        key = _env_get(ENV_OPENAI_API_KEY, sources)
        if key:
            out[ENV_OPENAI_API_KEY] = key
        # OPENAI_BASE_URL can redirect the OpenAI provider to a compatible gateway.
        if base := _env_get(ENV_OPENAI_BASE_URL, sources):
            out[ENV_OPENAI_BASE_URL] = base
        return out

    if provider == "anthropic":
        key = _compat_anthropic_token(sources)
        if key:
            out[ENV_ANTHROPIC_API_KEY] = key
        # Official Anthropic needs no base URL. ANTHROPIC_BASE_URL selects a gateway.
        if explicit := _env_get(ENV_ANTHROPIC_BASE_URL, sources):
            out[ENV_ANTHROPIC_BASE_URL] = explicit
        return out

    if provider == "deepseek":
        key = _env_get(ENV_DEEPSEEK_API_KEY, sources)
        if not key:
            return out
        out[ENV_DEEPSEEK_API_KEY] = key
        if family == "anthropic":
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
    key = resolve_custom_api_key(sources)
    if key:
        out[ENV_CUSTOM_API_KEY] = key
    # Unauthenticated custom endpoints still need a non-empty family key for
    # the model proxy to send.
    if base and not key:
        key = CUSTOM_UNAUTHENTICATED_API_KEY
    model = resolve_custom_model(sources)
    if model:
        out[ENV_CUSTOM_MODEL] = model

    if family == "anthropic":
        if key:
            out[ENV_ANTHROPIC_API_KEY] = key
            out[ENV_ANTHROPIC_AUTH_TOKEN] = key
        if base:
            out[ENV_ANTHROPIC_BASE_URL] = openai_compat_to_anthropic_base_url(base)
    else:
        if key:
            out[ENV_OPENAI_API_KEY] = key
        if base:
            out[ENV_OPENAI_BASE_URL] = base
    return out


def host_model_endpoint(
    provider: str, *, base_url: str | None = None
) -> tuple[str | None, str | None]:
    """``(base_url, api_key)`` for a model client NIKA itself runs on the host.

    ``None`` leaves the value to the client's standard variables.
    """
    if provider == "custom":
        url = (base_url or "").strip() or resolve_custom_base_url()
        if not url:
            from nika.run_config.loader import get_run_config

            url = (get_run_config().agent.base_url or "").strip()
        if not url:
            raise ValueError(
                "Missing agent.base_url: set it in config/nika.yaml "
                "when agent.provider is custom."
            )
        return url, resolve_custom_api_key() or None
    if provider == "deepseek":
        return DEEPSEEK_OPENAI_BASE_URL, os.environ.get(ENV_DEEPSEEK_API_KEY) or None
    return None, None


def host_llm_client() -> "LlmClientSettings":
    """Model client settings (``agent.llm``) for clients NIKA runs on the host."""
    from agent.runtime.task import LlmClientSettings
    from nika.run_config.loader import get_run_config

    llm = get_run_config().agent.llm
    return LlmClientSettings(
        timeout_sec=float(llm.timeout_sec),
        max_retries=int(llm.max_retries),
        stream=bool(llm.stream),
    )


_served_max_model_lens: dict[tuple[str, str], int] = {}


def _served_max_model_len(base_url: str, model: str) -> int | None:
    """Read ``max_model_len`` for *model* from an OpenAI-compatible ``/models``.

    Only successful lookups are cached so a transient probe failure is retried.
    """
    import json
    import urllib.request

    key = (base_url, model)
    if key in _served_max_model_lens:
        return _served_max_model_lens[key]
    url = base_url.rstrip("/")
    if not url.endswith("/v1"):
        url += "/v1"
    request = urllib.request.Request(f"{url}/models")
    if api_key := resolve_custom_api_key():
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            data = json.load(resp).get("data") or []
    except (OSError, ValueError, AttributeError):
        return None
    for entry in data:
        if isinstance(entry, dict) and model in (entry.get("id"), entry.get("root")):
            value = entry.get("max_model_len")
            if isinstance(value, int) and value > 0:
                _served_max_model_lens[key] = value
                return value
    return None


def custom_model_claude_env(model: str) -> dict[str, str]:
    """Context limits for a self-hosted model that Claude Code does not know.

    Claude Code assumes a 200k window and 32k output tokens for unknown models,
    so it neither auto-compacts nor trims ``max_tokens`` before a smaller
    server limit (vLLM ``max_model_len``) rejects the request. The host probes
    the endpoint, which the sandbox reaches only through the model proxy.
    """
    base_url = resolve_custom_base_url()
    if not base_url:
        from nika.run_config.loader import get_run_config

        base_url = (get_run_config().agent.base_url or "").strip()
    window = _served_max_model_len(base_url, model) if base_url else None
    if not window:
        return {}
    return {
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": str(window),
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(min(32000, window // 8)),
    }
