"""Sync API credentials into the sbx secret store without copying auth files.

NIKA reads only the active agent and provider credentials from ``.env``.
Claude/DeepSeek API-key mode uses ``sbx secret set-custom`` so credentials never
enter the microVM. Codex uses OpenAI via the built-in ``openai`` service
(``OPENAI_API_KEY`` or OAuth), or mapped DeepSeek and custom keys. Interactive
host login stores subscription credentials in the built-in services.
"""

from __future__ import annotations

import fcntl
import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import httpx

from agent.registry import BUILTIN_AGENTS
from agent.sandbox.config import load_credential_sources
from agent.sandbox.sbx.auth import PROXY_MANAGED_SENTINEL
from agent.sandbox.sbx.client import (
    list_sbx_custom_secrets,
    list_sbx_custom_secret_entries,
    list_sbx_secret_services,
    run_sbx_checked,
    sbx_available,
)
from agent.utils.provider_env import (
    CUSTOM_UNAUTHENTICATED_API_KEY,
    DEEPSEEK_ANTHROPIC_BASE_URL,
    DEEPSEEK_OPENAI_BASE_URL,
    ENV_ANTHROPIC_API_KEY,
    ENV_ANTHROPIC_AUTH_TOKEN,
    ENV_ANTHROPIC_BASE_URL,
    ENV_CUSTOM_API_KEY,
    ENV_DEEPSEEK_API_KEY,
    ENV_OPENAI_API_KEY,
    resolve_custom_api_key,
    resolve_custom_base_url,
)

logger = logging.getLogger(__name__)

SERVICE_OPENAI = "openai"
SERVICE_ANTHROPIC = "anthropic"

DEEPSEEK_HOST = "api.deepseek.com"

# Sandbox agents need the credential service of their client family.
_AGENT_REQUIRED_SERVICES: dict[str, frozenset[str]] = {
    spec.id: frozenset({spec.credential_family})
    for spec in BUILTIN_AGENTS
    if spec.sbx is not None
}

_OFFICIAL_ANTHROPIC_HOST_SUFFIXES = (
    "anthropic.com",
    "claude.ai",
)


@dataclass(frozen=True)
class SbxCredentialPlan:
    """Host-side credential plan for a sandbox session."""

    services: frozenset[str]
    api_key_services: frozenset[str]
    anthropic_base_url: str = ""
    openai_base_url: str = ""
    third_party_anthropic: bool = False
    third_party_openai: bool = False
    custom_placeholders: dict[str, str] = field(default_factory=dict)
    provider: str = ""

    @property
    def openai_api_key_mode(self) -> bool:
        return SERVICE_OPENAI in self.api_key_services

    @property
    def anthropic_api_key_mode(self) -> bool:
        return SERVICE_ANTHROPIC in self.api_key_services

    def sentinel_runtime_env(self) -> dict[str, str]:
        """Env vars for the sandbox (placeholders / sentinels only)."""
        env: dict[str, str] = {}
        if self.openai_api_key_mode:
            if self.third_party_openai:
                env["OPENAI_API_KEY"] = self.custom_placeholders.get(
                    "OPENAI_API_KEY", PROXY_MANAGED_SENTINEL
                )
                if self.openai_base_url:
                    env["OPENAI_BASE_URL"] = self.openai_base_url
            else:
                env["OPENAI_API_KEY"] = PROXY_MANAGED_SENTINEL
        if self.third_party_anthropic:
            placeholder = self.custom_placeholders.get(
                "ANTHROPIC_API_KEY", PROXY_MANAGED_SENTINEL
            )
            # DeepSeek Claude Code docs use ANTHROPIC_AUTH_TOKEN; sbx set-custom
            # is registered for ANTHROPIC_API_KEY. Export the same placeholder
            # under both names so Claude CLI/SDK can auth either way.
            env["ANTHROPIC_API_KEY"] = placeholder
            env["ANTHROPIC_AUTH_TOKEN"] = placeholder
            env["ANTHROPIC_BASE_URL"] = (
                self.anthropic_base_url or DEEPSEEK_ANTHROPIC_BASE_URL
            )
        elif self.anthropic_api_key_mode:
            env["ANTHROPIC_API_KEY"] = PROXY_MANAGED_SENTINEL
            if self.anthropic_base_url:
                env["ANTHROPIC_BASE_URL"] = self.anthropic_base_url
        return env


def required_services_for_agent(agent_type: str) -> frozenset[str]:
    return _AGENT_REQUIRED_SERVICES.get(agent_type, frozenset())


def _require_provider(provider: str | None) -> str:
    if not provider or not str(provider).strip():
        raise ValueError(
            "Missing LLM provider for sandbox credentials: set agent.provider "
            "in config/nika.yaml or pass -p/--provider."
        )
    return str(provider).strip().lower()


def is_official_anthropic_base_url(base_url: str) -> bool:
    """True when *base_url* is empty (default Anthropic) or an official host."""
    if not base_url.strip():
        return True
    host = (urlparse(base_url).hostname or "").lower()
    if not host:
        return True
    return any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _OFFICIAL_ANTHROPIC_HOST_SUFFIXES
    )


def _anthropic_base_url(sources: dict[str, str], provider: str) -> str:
    from agent.utils.provider_env import openai_compat_to_anthropic_base_url

    raw = sources.get(ENV_ANTHROPIC_BASE_URL, "").strip()
    if raw:
        return raw
    if provider == "deepseek" or sources.get(ENV_DEEPSEEK_API_KEY, "").strip():
        return DEEPSEEK_ANTHROPIC_BASE_URL
    if provider == "custom":
        custom = resolve_custom_base_url(sources)
        return openai_compat_to_anthropic_base_url(custom) if custom else ""
    return ""


def _openai_base_url(sources: dict[str, str], provider: str) -> str:
    raw = sources.get("OPENAI_BASE_URL", "").strip()
    if raw:
        return raw
    if provider == "deepseek":
        return DEEPSEEK_OPENAI_BASE_URL
    if provider == "custom":
        return resolve_custom_base_url(sources)
    return ""


def _anthropic_secret_value(sources: dict[str, str]) -> str:
    return (
        sources.get(ENV_ANTHROPIC_API_KEY, "").strip()
        or sources.get(ENV_ANTHROPIC_AUTH_TOKEN, "").strip()
        or sources.get(ENV_DEEPSEEK_API_KEY, "").strip()
        or sources.get(ENV_CUSTOM_API_KEY, "").strip()
    )


def _openai_secret_value(sources: dict[str, str]) -> str:
    return (
        sources.get(ENV_OPENAI_API_KEY, "").strip()
        or sources.get(ENV_DEEPSEEK_API_KEY, "").strip()
        or resolve_custom_api_key(sources)
    )


def require_anthropic_messages_route(base_url: str, *, api_key: str = "") -> None:
    """Fail fast when a custom endpoint has no Anthropic ``/v1/messages`` route.

    Claude-family agents only speak the Anthropic Messages API. The probe sends
    an empty body, so a server with the route rejects it (4xx) without running
    the model; only HTTP 404 means the route is missing. Network errors are
    left to the agent run.
    """
    url = base_url.rstrip("/") + "/v1/messages"
    headers = {"anthropic-version": "2023-06-01"}
    if api_key and api_key != CUSTOM_UNAUTHENTICATED_API_KEY:
        headers["x-api-key"] = api_key
        headers["authorization"] = f"Bearer {api_key}"
    try:
        status = httpx.post(url, json={}, headers=headers, timeout=10.0).status_code
    except httpx.HTTPError:
        return
    if status == 404:
        raise RuntimeError(
            f"Custom endpoint {base_url} has no Anthropic Messages route "
            f"(POST {url} returned 404).\n"
            "Claude-family agents (cli.claude, sdk.claude_sdk, community.sade) "
            "need /v1/messages: upgrade Ollama to 0.14 or later, use vLLM, or "
            "run an OpenAI-family agent (byo.*, cli.codex, sdk.codex_sdk) "
            "against this endpoint."
        )


def missing_credential_message(service: str, *, provider: str = "") -> str:
    if service == SERVICE_OPENAI:
        return (
            "Missing Docker Sandboxes credential for Codex.\n"
            "API key: set OPENAI_API_KEY or DEEPSEEK_API_KEY in the repository-root "
            ".env. For a custom endpoint, set agent.base_url and optional "
            "NIKA_CUSTOM_API_KEY. Set agent.provider to "
            f"{provider or 'openai|deepseek|custom'} in config/nika.yaml.\n"
            "ChatGPT / Codex subscription: run "
            "`sbx secret set -g openai --oauth` once on the host.\n"
            "See docs/operations/agent-sandbox.md and "
            "https://docs.docker.com/ai/sandboxes/agents/codex/"
        )
    if service == SERVICE_ANTHROPIC:
        return (
            "Missing Docker Sandboxes credential for Claude.\n"
            "Native Anthropic: set ANTHROPIC_API_KEY and agent.provider: anthropic.\n"
            "DeepSeek: set DEEPSEEK_API_KEY and agent.provider: deepseek.\n"
            "Custom proxy: set NIKA_CUSTOM_API_KEY (optional) with "
            "agent.base_url and agent.provider: custom in config/nika.yaml.\n"
            "Claude subscription: authenticate with `/login` inside Claude Code "
            "so the anthropic secret is stored on the host "
            "(see https://docs.docker.com/ai/sandboxes/agents/claude-code/).\n"
            "See docs/operations/agent-sandbox.md"
        )
    return f"Missing Docker Sandboxes credential for {service}."


def _host_covered(targets: str, host: str) -> bool:
    """True when *host* is already listed in a custom-secret TARGETS cell."""
    wanted = host.strip().lower()
    if not wanted:
        return False
    for item in targets.replace(",", " ").split():
        token = item.strip().lower()
        if not token:
            continue
        if (
            token == wanted
            or token == f"*.{wanted}"
            or wanted.endswith("." + token.lstrip("*."))
        ):
            return True
        # Exact wildcard forms from sbx docs: *.example.com / **.example.com
        if token.startswith("*.") and (
            wanted == token[2:] or wanted.endswith("." + token[2:])
        ):
            return True
    return False


def _ensure_custom_secret(
    *,
    host: str,
    env_name: str,
    value: str,
    existing: dict[str, str],
) -> str:
    """Create or replace a set-custom secret; return its placeholder.

    ``sbx secret set-custom`` refuses to overwrite an existing env binding, and
    the proxy only injects the secret for the registered target host. When the
    same env (e.g. ``OPENAI_API_KEY``) was previously bound to another host
    (DeepSeek vs OpenRouter), remove the stale placeholder and recreate.
    """
    entries = list_sbx_custom_secret_entries()
    entry = entries.get(env_name)
    if entry and _host_covered(entry.get("hosts", ""), host):
        return entry["placeholder"]
    if entry:
        run_sbx_checked(["secret", "rm", "--placeholder", entry["placeholder"], "-f"])
    run_sbx_checked(
        [
            "secret",
            "set-custom",
            "--host",
            host,
            "--env",
            env_name,
            "--value",
            value,
        ]
    )
    refreshed = list_sbx_custom_secrets()
    placeholder = refreshed.get(env_name, "").strip()
    if not placeholder:
        raise RuntimeError(
            f"sbx secret set-custom succeeded but no placeholder for {env_name}"
        )
    logger.debug("Synced custom sbx secret env=%s host=%s", env_name, host)
    return placeholder


@contextmanager
def _credentials_lock() -> Iterator[None]:
    """Serialize credential sync across processes (parallel benchmark trials).

    Syncing reads the sbx secrets and then creates missing ones; two trials
    doing that at once both try to create the same secret, and sbx rejects the
    second (``already exists``).
    """
    path = Path(tempfile.gettempdir()) / f"nika-sbx-credentials-{os.getuid()}.lock"
    with open(path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def ensure_sbx_credentials(
    *,
    env_file: Path,
    required_services: frozenset[str] | set[str] | None = None,
    provider: str | None = None,
    agent_type: str = "cli.claude",
) -> SbxCredentialPlan:
    """Import API keys from .env into sbx secrets; OAuth remains user-owned.

    Only credentials for the active provider are synced.
    """
    with _credentials_lock():
        return _ensure_sbx_credentials(
            env_file=env_file,
            required_services=required_services,
            provider=provider,
            agent_type=agent_type,
        )


def _ensure_sbx_credentials(
    *,
    env_file: Path,
    required_services: frozenset[str] | set[str] | None,
    provider: str | None,
    agent_type: str,
) -> SbxCredentialPlan:
    if not sbx_available():
        raise RuntimeError("sbx is required for sandbox credential sync")

    required = frozenset(required_services or ())
    prov = _require_provider(provider)
    sources = load_credential_sources(env_file, provider=prov, agent_type=agent_type)
    base_url = _anthropic_base_url(sources, prov)
    openai_base = _openai_base_url(sources, prov)
    third_party_anth = bool(base_url) and not is_official_anthropic_base_url(base_url)
    third_party_oai = bool(openai_base) and "api.openai.com" not in openai_base
    existing_services = list_sbx_secret_services()
    custom = list_sbx_custom_secrets()
    placeholders: dict[str, str] = {}
    api_key_services: set[str] = set()
    satisfied: set[str] = set(existing_services)
    third_party_synced = False
    third_party_oai_synced = False

    need_openai = not required or SERVICE_OPENAI in required
    need_anthropic = not required or SERVICE_ANTHROPIC in required

    # The official endpoints take only their own provider's key: a DeepSeek or
    # custom key in .env must never become the global sbx openai/anthropic
    # secret, nor turn a subscription login into API-key mode.
    official_openai_key = sources.get(ENV_OPENAI_API_KEY, "").strip()
    official_anthropic_key = (
        sources.get(ENV_ANTHROPIC_API_KEY, "").strip()
        or sources.get(ENV_ANTHROPIC_AUTH_TOKEN, "").strip()
    )

    openai_key = _openai_secret_value(sources) if need_openai else ""
    if need_openai:
        if third_party_oai and openai_base:
            # Custom / third-party OpenAI-compat endpoint: always forward
            # OPENAI_BASE_URL into the sandbox. Unauthenticated endpoints use
            # a sentinel key without registering an sbx secret.
            if openai_key and openai_key != CUSTOM_UNAUTHENTICATED_API_KEY:
                host = urlparse(openai_base).hostname or DEEPSEEK_HOST
                placeholders["OPENAI_API_KEY"] = _ensure_custom_secret(
                    host=host,
                    env_name="OPENAI_API_KEY",
                    value=openai_key,
                    existing=custom,
                )
            else:
                placeholders["OPENAI_API_KEY"] = (
                    openai_key or CUSTOM_UNAUTHENTICATED_API_KEY
                )
            third_party_oai_synced = True
            api_key_services.add(SERVICE_OPENAI)
            satisfied.add(SERVICE_OPENAI)
        elif official_openai_key:
            if SERVICE_OPENAI not in existing_services:
                run_sbx_checked(
                    ["secret", "set", "-g", SERVICE_OPENAI],
                    input_text=official_openai_key,
                )
            api_key_services.add(SERVICE_OPENAI)
            satisfied.add(SERVICE_OPENAI)
        elif SERVICE_OPENAI in existing_services:
            satisfied.add(SERVICE_OPENAI)

    anthropic_key = _anthropic_secret_value(sources) if need_anthropic else ""
    if need_anthropic:
        if third_party_anth and base_url:
            if prov == "custom" and SERVICE_ANTHROPIC in required:
                require_anthropic_messages_route(base_url, api_key=anthropic_key)
            if anthropic_key and anthropic_key != CUSTOM_UNAUTHENTICATED_API_KEY:
                host = urlparse(base_url).hostname or DEEPSEEK_HOST
                placeholders["ANTHROPIC_API_KEY"] = _ensure_custom_secret(
                    host=host,
                    env_name="ANTHROPIC_API_KEY",
                    value=anthropic_key,
                    existing=custom,
                )
            else:
                placeholders["ANTHROPIC_API_KEY"] = (
                    anthropic_key or CUSTOM_UNAUTHENTICATED_API_KEY
                )
            third_party_synced = True
            api_key_services.add(SERVICE_ANTHROPIC)
            satisfied.add(SERVICE_ANTHROPIC)
        elif official_anthropic_key and not third_party_anth:
            if SERVICE_ANTHROPIC not in existing_services:
                run_sbx_checked(
                    ["secret", "set", "-g", SERVICE_ANTHROPIC],
                    input_text=official_anthropic_key,
                )
            api_key_services.add(SERVICE_ANTHROPIC)
            satisfied.add(SERVICE_ANTHROPIC)
        elif SERVICE_ANTHROPIC in existing_services:
            satisfied.add(SERVICE_ANTHROPIC)

    missing = required - frozenset(satisfied)
    if missing:
        parts = [
            missing_credential_message(service, provider=prov)
            for service in sorted(missing)
        ]
        raise RuntimeError("\n\n".join(parts))

    return SbxCredentialPlan(
        services=frozenset(satisfied),
        api_key_services=frozenset(api_key_services),
        anthropic_base_url=base_url if need_anthropic else "",
        openai_base_url=openai_base if need_openai else "",
        third_party_anthropic=bool(third_party_synced and need_anthropic),
        third_party_openai=bool(third_party_oai_synced and need_openai),
        custom_placeholders=placeholders,
        provider=prov,
    )


def sbx_openai_credential_available(*, env_file: Path | None = None) -> bool:
    if os.environ.get(ENV_OPENAI_API_KEY, "").strip():
        return True
    if os.environ.get(ENV_DEEPSEEK_API_KEY, "").strip():
        return True
    if resolve_custom_api_key():
        return True
    if env_file is not None:
        sources = load_credential_sources(
            env_file, provider="openai", agent_type="cli.codex"
        )
        if _openai_secret_value(sources):
            return True
    if not sbx_available():
        return False
    try:
        if SERVICE_OPENAI in list_sbx_secret_services():
            return True
        return "OPENAI_API_KEY" in list_sbx_custom_secrets()
    except RuntimeError:
        return False


def sbx_anthropic_credential_available(*, env_file: Path | None = None) -> bool:
    if (
        os.environ.get(ENV_ANTHROPIC_API_KEY, "").strip()
        or os.environ.get(ENV_ANTHROPIC_AUTH_TOKEN, "").strip()
        or os.environ.get(ENV_DEEPSEEK_API_KEY, "").strip()
        or resolve_custom_api_key()
    ):
        return True
    if env_file is not None:
        sources = load_credential_sources(
            env_file,
            provider="anthropic",
            agent_type="cli.claude",
        )
        if _anthropic_secret_value(sources):
            return True
    if not sbx_available():
        return False
    try:
        if SERVICE_ANTHROPIC in list_sbx_secret_services():
            return True
        return "ANTHROPIC_API_KEY" in list_sbx_custom_secrets()
    except RuntimeError:
        return False


def anthropic_subscription_mode(*, env_file: Path | None = None) -> bool:
    """True when Anthropic is available via sbx secret without env API keys."""
    if (
        os.environ.get(ENV_ANTHROPIC_API_KEY, "").strip()
        or os.environ.get(ENV_ANTHROPIC_AUTH_TOKEN, "").strip()
        or os.environ.get(ENV_DEEPSEEK_API_KEY, "").strip()
        or resolve_custom_api_key()
    ):
        return False
    if env_file is not None:
        sources = load_credential_sources(
            env_file, provider="anthropic", agent_type="cli.claude"
        )
        if _anthropic_secret_value(sources):
            return False
    if not sbx_available():
        return False
    try:
        return SERVICE_ANTHROPIC in list_sbx_secret_services()
    except RuntimeError:
        return False
