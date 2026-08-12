"""Sync API credentials into the sbx secret store (no host auth file copies).

Only the active agent + ``NIKA_LLM_PROVIDER`` credentials are read from ``.env``.
Claude/DeepSeek API-key mode uses ``sbx secret set-custom`` so credentials never
enter the microVM. Codex uses OpenAI via the built-in ``openai`` service
(``OPENAI_API_KEY`` or OAuth), or mapped DeepSeek/custom keys. Subscription /
OAuth still uses built-in ``openai`` / ``anthropic`` services with interactive
host login.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from agent.sandbox.config import load_sandbox_env_values
from agent.sandbox.sbx.auth import PROXY_MANAGED_SENTINEL
from agent.sandbox.sbx.client import (
    list_sbx_custom_secrets,
    list_sbx_secret_services,
    run_sbx_checked,
    sbx_available,
)
from agent.utils.provider_env import (
    DEEPSEEK_ANTHROPIC_BASE_URL,
    DEEPSEEK_OPENAI_BASE_URL,
    ENV_ANTHROPIC_API_KEY,
    ENV_ANTHROPIC_AUTH_TOKEN,
    ENV_ANTHROPIC_BASE_URL,
    ENV_CUSTOM_API_KEY,
    ENV_CUSTOM_BASE_URL,
    ENV_DEEPSEEK_API_KEY,
    ENV_OPENAI_API_KEY,
    map_provider_credentials,
    resolve_custom_api_key,
    resolve_custom_base_url,
)

logger = logging.getLogger(__name__)

SERVICE_OPENAI = "openai"
SERVICE_ANTHROPIC = "anthropic"

DEEPSEEK_HOST = "api.deepseek.com"

_AGENT_REQUIRED_SERVICES: dict[str, frozenset[str]] = {
    "cli.codex": frozenset({SERVICE_OPENAI}),
    "sdk.codex_sdk": frozenset({SERVICE_OPENAI}),
    "cli.claude": frozenset({SERVICE_ANTHROPIC}),
    "sdk.claude_sdk": frozenset({SERVICE_ANTHROPIC}),
    "community.sade": frozenset({SERVICE_ANTHROPIC}),
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
        if self.provider:
            env["NIKA_LLM_PROVIDER"] = self.provider
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


def _active_provider(provider: str | None = None) -> str:
    if provider and provider.strip():
        return provider.strip().lower()
    return (os.environ.get("NIKA_LLM_PROVIDER") or "").strip().lower()


def _credential_sources(
    env_file: Path, *, provider: str, agent_type: str
) -> dict[str, str]:
    """Load .env then keep only keys needed for the active provider mapping."""
    file_vals = load_sandbox_env_values(env_file)
    # Overlay live process env for the known credential keys
    merged = dict(file_vals)
    for key in (
        ENV_DEEPSEEK_API_KEY,
        ENV_OPENAI_API_KEY,
        ENV_ANTHROPIC_API_KEY,
        ENV_ANTHROPIC_AUTH_TOKEN,
        ENV_ANTHROPIC_BASE_URL,
        ENV_CUSTOM_BASE_URL,
        ENV_CUSTOM_API_KEY,
        "CUSTOM_API_BASE",
        "CUSTOM_API_KEY",
        "NIKA_CUSTOM_MODEL",
        "NIKA_LLM_PROVIDER",
    ):
        value = os.environ.get(key, "").strip()
        if value:
            merged[key] = value

    if not provider:
        return merged

    mapped = map_provider_credentials(
        agent_type=agent_type, provider=provider, sources=merged
    )
    # Keep only mapped credentials + non-secret provider markers
    allowed = set(mapped) | {
        "NIKA_LLM_PROVIDER",
        ENV_CUSTOM_BASE_URL,
        ENV_CUSTOM_API_KEY,
        "NIKA_CUSTOM_MODEL",
    }
    return {k: v for k, v in {**merged, **mapped}.items() if k in allowed and v}


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
    raw = sources.get(ENV_ANTHROPIC_BASE_URL, "").strip()
    if raw:
        return raw
    if provider == "deepseek" or sources.get(ENV_DEEPSEEK_API_KEY, "").strip():
        return DEEPSEEK_ANTHROPIC_BASE_URL
    if provider == "custom":
        return resolve_custom_base_url(sources)
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


def missing_credential_message(service: str, *, provider: str = "") -> str:
    if service == SERVICE_OPENAI:
        return (
            "Missing Docker Sandboxes credential for Codex.\n"
            "API key: set OPENAI_API_KEY (or DEEPSEEK_API_KEY / NIKA_CUSTOM_* "
            f"with NIKA_LLM_PROVIDER={provider or 'openai|deepseek|custom'}) "
            "in the repo-root .env.\n"
            "ChatGPT / Codex subscription: run "
            "`sbx secret set -g openai --oauth` once on the host.\n"
            "See docs/agent-sandbox.md and "
            "https://docs.docker.com/ai/sandboxes/agents/codex/"
        )
    if service == SERVICE_ANTHROPIC:
        return (
            "Missing Docker Sandboxes credential for Claude.\n"
            "Native Anthropic: set ANTHROPIC_API_KEY.\n"
            "DeepSeek: set DEEPSEEK_API_KEY and NIKA_LLM_PROVIDER=deepseek.\n"
            "Custom proxy: set NIKA_CUSTOM_BASE_URL (+ optional NIKA_CUSTOM_API_KEY) "
            "and NIKA_LLM_PROVIDER=custom.\n"
            "Claude subscription: authenticate with `/login` inside Claude Code "
            "so the anthropic secret is stored on the host "
            "(see https://docs.docker.com/ai/sandboxes/agents/claude-code/).\n"
            "See docs/agent-sandbox.md"
        )
    return f"Missing Docker Sandboxes credential for {service}."


def _ensure_custom_secret(
    *,
    host: str,
    env_name: str,
    value: str,
    existing: dict[str, str],
) -> str:
    """Create or reuse a set-custom secret; return its placeholder."""
    if env_name in existing:
        return existing[env_name]
    run_sbx_checked(
        [
            "secret",
            "set-custom",
            "-g",
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
    if not sbx_available():
        raise RuntimeError("sbx is required for sandbox credential sync")

    required = frozenset(required_services or ())
    prov = _active_provider(provider)
    sources = _credential_sources(env_file, provider=prov, agent_type=agent_type)
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

    openai_key = _openai_secret_value(sources) if need_openai else ""
    if need_openai:
        if openai_key and third_party_oai:
            host = urlparse(openai_base).hostname or DEEPSEEK_HOST
            placeholders["OPENAI_API_KEY"] = _ensure_custom_secret(
                host=host,
                env_name="OPENAI_API_KEY",
                value=openai_key,
                existing=custom,
            )
            third_party_oai_synced = True
            api_key_services.add(SERVICE_OPENAI)
            satisfied.add(SERVICE_OPENAI)
        elif openai_key:
            if SERVICE_OPENAI not in existing_services:
                run_sbx_checked(
                    ["secret", "set", "-g", SERVICE_OPENAI],
                    input_text=openai_key,
                )
            api_key_services.add(SERVICE_OPENAI)
            satisfied.add(SERVICE_OPENAI)
        elif SERVICE_OPENAI in existing_services:
            satisfied.add(SERVICE_OPENAI)

    anthropic_key = _anthropic_secret_value(sources) if need_anthropic else ""
    if need_anthropic:
        if anthropic_key and third_party_anth:
            host = urlparse(base_url).hostname or DEEPSEEK_HOST
            placeholders["ANTHROPIC_API_KEY"] = _ensure_custom_secret(
                host=host,
                env_name="ANTHROPIC_API_KEY",
                value=anthropic_key,
                existing=custom,
            )
            third_party_synced = True
            api_key_services.add(SERVICE_ANTHROPIC)
            satisfied.add(SERVICE_ANTHROPIC)
        elif anthropic_key and not third_party_anth:
            if SERVICE_ANTHROPIC not in existing_services:
                run_sbx_checked(
                    ["secret", "set", "-g", SERVICE_ANTHROPIC],
                    input_text=anthropic_key,
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
    provider = _active_provider()
    if provider in ("openai", "deepseek", "custom") and provider:
        from agent.utils.provider_env import has_provider_credentials

        if has_provider_credentials(provider):
            return True
    if os.environ.get(ENV_OPENAI_API_KEY, "").strip():
        return True
    if env_file is not None:
        sources = _credential_sources(
            env_file, provider=provider or "openai", agent_type="cli.codex"
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
    provider = _active_provider()
    if provider in ("anthropic", "deepseek", "custom"):
        from agent.utils.provider_env import has_provider_credentials

        if has_provider_credentials(provider):
            return True
    if (
        os.environ.get(ENV_ANTHROPIC_API_KEY, "").strip()
        or os.environ.get(ENV_ANTHROPIC_AUTH_TOKEN, "").strip()
        or os.environ.get(ENV_DEEPSEEK_API_KEY, "").strip()
    ):
        return True
    if env_file is not None:
        sources = _credential_sources(
            env_file,
            provider=provider or "anthropic",
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
        provider = _active_provider() or "anthropic"
        sources = _credential_sources(
            env_file, provider=provider, agent_type="cli.claude"
        )
        if _anthropic_secret_value(sources):
            return False
    if not sbx_available():
        return False
    try:
        return SERVICE_ANTHROPIC in list_sbx_secret_services()
    except RuntimeError:
        return False
