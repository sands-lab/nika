"""Check that an agent's LLM endpoint is reachable and serves the configured model.

The probe lists models (``GET <api root>/models``) with the same credentials the
agent will use. It sends no completion request, so it costs no tokens.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

from urllib.parse import urlsplit

import httpx

from nika.agents.provider_env import (
    DEEPSEEK_OPENAI_BASE_URL,
    ENV_ANTHROPIC_API_KEY,
    ENV_ANTHROPIC_AUTH_TOKEN,
    ENV_ANTHROPIC_BASE_URL,
    ENV_DEEPSEEK_API_KEY,
    ENV_DEEPSEEK_BASE_URL,
    ENV_OPENAI_API_KEY,
    ENV_OPENAI_BASE_URL,
    has_provider_credentials,
    openai_compat_to_anthropic_base_url,
    resolve_custom_api_key,
    resolve_custom_base_url,
)

OPENAI_API_ROOT = "https://api.openai.com/v1"
ANTHROPIC_API_ROOT = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

# status values, ordered from healthy to broken
OK = "ok"
MODEL_NOT_LISTED = "model_not_listed"
NO_CREDENTIALS = "no_credentials"
AUTH_FAILED = "auth_failed"
UNREACHABLE = "unreachable"
ERROR = "error"


@dataclass
class ProbeResult:
    provider: str
    model: str
    endpoint: str
    status: str
    detail: str = ""
    latency_ms: float | None = None
    models: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the agent can reach the endpoint with valid credentials."""
        return self.status in (OK, MODEL_NOT_LISTED)


def _env(key: str, sources: Mapping[str, str] | None) -> str:
    return ((sources if sources is not None else os.environ).get(key) or "").strip()


def _request_spec(
    provider: str, base_url: str | None, sources: Mapping[str, str] | None
) -> tuple[str, dict[str, str]]:
    match provider:
        case "openai":
            key = _env(ENV_OPENAI_API_KEY, sources)
            root = _env(ENV_OPENAI_BASE_URL, sources) or OPENAI_API_ROOT
            return root, {"Authorization": f"Bearer {key}"}
        case "deepseek":
            key = _env(ENV_DEEPSEEK_API_KEY, sources)
            root = _env(ENV_DEEPSEEK_BASE_URL, sources) or DEEPSEEK_OPENAI_BASE_URL
            return root, {"Authorization": f"Bearer {key}"}
        case "anthropic":
            headers = {"anthropic-version": ANTHROPIC_VERSION}
            if key := _env(ENV_ANTHROPIC_API_KEY, sources):
                headers["x-api-key"] = key
            else:
                token = _env(ENV_ANTHROPIC_AUTH_TOKEN, sources)
                headers["Authorization"] = f"Bearer {token}"
            explicit = _env(ENV_ANTHROPIC_BASE_URL, sources)
            root = (
                openai_compat_to_anthropic_base_url(explicit) + "/v1"
                if explicit
                else ANTHROPIC_API_ROOT
            )
            return root, headers
        case "custom":
            root = (base_url or "").strip() or resolve_custom_base_url(sources)
            headers = {}
            if key := resolve_custom_api_key(sources):
                headers["Authorization"] = f"Bearer {key}"
            return root, headers
    raise ValueError(f"Unsupported LLM provider {provider!r}.")


def _model_listed(model: str, models: list[str]) -> bool:
    # Anthropic aliases (claude-sonnet-4-5) resolve to dated ids; routers such as
    # OpenRouter prefix vendor names (deepseek/deepseek-v4-flash).
    return any(
        m == model or m.startswith(f"{model}-") or m.endswith(f"/{model}")
        for m in models
    )


def probe_endpoint(
    provider: str,
    model: str,
    *,
    base_url: str | None = None,
    timeout: float = 8.0,
    sources: Mapping[str, str] | None = None,
) -> ProbeResult:
    """List models at the provider endpoint and check that *model* is served."""
    provider = provider.strip().lower()
    model = (model or "").strip()
    root, headers = _request_spec(provider, base_url, sources)
    url = f"{root.rstrip('/')}/models"
    result = ProbeResult(provider=provider, model=model, endpoint=url, status=ERROR)
    if not root:
        result.status = NO_CREDENTIALS
        result.detail = "Set agent.base_url for provider custom."
        return result
    # Custom endpoints may be unauthenticated; the base URL is enough.
    if provider != "custom" and not has_provider_credentials(provider, sources):
        result.status = NO_CREDENTIALS
        result.detail = "API key missing from .env."
        return result
    params = {"limit": "1000"} if provider == "anthropic" else None
    start = time.monotonic()
    try:
        response = httpx.get(url, headers=headers, params=params, timeout=timeout)
    except httpx.HTTPError as exc:
        result.status = UNREACHABLE
        result.detail = f"{type(exc).__name__}: {exc}".rstrip(": ")
        return result
    result.latency_ms = (time.monotonic() - start) * 1000
    if response.status_code in (401, 403):
        result.status = AUTH_FAILED
        result.detail = f"HTTP {response.status_code}: credentials rejected."
        return result
    if response.status_code >= 400:
        result.status = ERROR
        result.detail = f"HTTP {response.status_code}: {response.text[:200]}"
        if (
            provider == "custom"
            and response.status_code == 404
            and urlsplit(root).path.strip("/") == ""
        ):
            result.detail = (
                f"HTTP 404 at {url}: agent.base_url has no API path; "
                f"did you mean {root.rstrip('/')}/v1?"
            )
        return result
    try:
        data = response.json().get("data") or []
        result.models = sorted(str(item.get("id")) for item in data if item.get("id"))
    except (ValueError, AttributeError):
        result.models = []
    if model and result.models and not _model_listed(model, result.models):
        result.status = MODEL_NOT_LISTED
        result.detail = f"Endpoint is up but does not list model {model!r}."
        return result
    result.status = OK
    result.detail = "Endpoint is up" + (
        f" and serves {model!r}." if model and result.models else "."
    )
    return result
