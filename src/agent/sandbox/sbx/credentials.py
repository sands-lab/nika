"""Sync API credentials into the sbx secret store (no host auth file copies).

Claude API-key mode prefers DeepSeek (``DEEPSEEK_API_KEY``) via ``sbx secret
set-custom`` so credentials never enter the microVM. Codex uses OpenAI via the
built-in ``openai`` service (``OPENAI_API_KEY`` or OAuth). Subscription / OAuth
still uses built-in ``openai`` / ``anthropic`` services with interactive host login.
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

logger = logging.getLogger(__name__)

SERVICE_OPENAI = "openai"
SERVICE_ANTHROPIC = "anthropic"

DEEPSEEK_HOST = "api.deepseek.com"
DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"

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
    third_party_anthropic: bool = False
    custom_placeholders: dict[str, str] = field(default_factory=dict)

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
            env["OPENAI_API_KEY"] = PROXY_MANAGED_SENTINEL
        if self.third_party_anthropic:
            env["ANTHROPIC_API_KEY"] = self.custom_placeholders.get(
                "ANTHROPIC_API_KEY", PROXY_MANAGED_SENTINEL
            )
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


def _credential_sources(env_file: Path) -> dict[str, str]:
    merged = load_sandbox_env_values(env_file)
    for key in (
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
    ):
        value = os.environ.get(key, "").strip()
        if value:
            merged[key] = value
    return merged


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


def _anthropic_base_url(sources: dict[str, str]) -> str:
    raw = sources.get("ANTHROPIC_BASE_URL", "").strip()
    if raw:
        return raw
    if (
        sources.get("DEEPSEEK_API_KEY", "").strip()
        or sources.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    ):
        # Default Anthropic-compatible DeepSeek endpoint for API-key runs.
        return DEEPSEEK_ANTHROPIC_BASE_URL
    return ""


def _anthropic_secret_value(sources: dict[str, str]) -> str:
    return (
        sources.get("ANTHROPIC_API_KEY", "").strip()
        or sources.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        or sources.get("DEEPSEEK_API_KEY", "").strip()
    )


def _openai_secret_value(sources: dict[str, str]) -> str:
    return sources.get("OPENAI_API_KEY", "").strip()


def missing_credential_message(service: str) -> str:
    if service == SERVICE_OPENAI:
        return (
            "Missing Docker Sandboxes credential for Codex.\n"
            "API key: set OPENAI_API_KEY in the repo-root .env "
            "(synced into the built-in openai sbx secret).\n"
            "ChatGPT / Codex subscription: run "
            "`sbx secret set -g openai --oauth` once on the host.\n"
            "See docs/agent-sandbox.md and "
            "https://docs.docker.com/ai/sandboxes/agents/codex/"
        )
    if service == SERVICE_ANTHROPIC:
        return (
            "Missing Docker Sandboxes credential for Claude.\n"
            "API key (DeepSeek): set DEEPSEEK_API_KEY or ANTHROPIC_AUTH_TOKEN "
            "(+ ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic) in .env.\n"
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
) -> SbxCredentialPlan:
    """Import API keys from .env into sbx secrets; OAuth remains user-owned.

    Claude API-key mode uses DeepSeek via ``set-custom`` (placeholder in the VM).
    Codex uses the built-in ``openai`` service (``OPENAI_API_KEY`` or OAuth).
    Subscription mode uses built-in ``openai`` / ``anthropic`` secrets only.
    """
    if not sbx_available():
        raise RuntimeError("sbx is required for sandbox credential sync")

    required = frozenset(required_services or ())
    sources = _credential_sources(env_file)
    base_url = _anthropic_base_url(sources)
    third_party = bool(base_url) and not is_official_anthropic_base_url(base_url)
    existing_services = list_sbx_secret_services()
    custom = list_sbx_custom_secrets()
    placeholders: dict[str, str] = {}
    api_key_services: set[str] = set()
    satisfied: set[str] = set(existing_services)
    third_party_synced = False

    need_openai = not required or SERVICE_OPENAI in required
    need_anthropic = not required or SERVICE_ANTHROPIC in required

    openai_key = _openai_secret_value(sources)
    if need_openai:
        if openai_key:
            if SERVICE_OPENAI not in existing_services:
                run_sbx_checked(
                    ["secret", "set", "-g", SERVICE_OPENAI],
                    input_text=openai_key,
                )
            api_key_services.add(SERVICE_OPENAI)
            satisfied.add(SERVICE_OPENAI)
        elif SERVICE_OPENAI in existing_services:
            satisfied.add(SERVICE_OPENAI)

    anthropic_key = _anthropic_secret_value(sources)
    if need_anthropic:
        if anthropic_key and third_party:
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
        elif anthropic_key and not third_party:
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
        parts = [missing_credential_message(service) for service in sorted(missing)]
        raise RuntimeError("\n\n".join(parts))

    return SbxCredentialPlan(
        services=frozenset(satisfied),
        api_key_services=frozenset(api_key_services),
        anthropic_base_url=base_url if need_anthropic else "",
        third_party_anthropic=bool(third_party_synced and need_anthropic),
        custom_placeholders=placeholders,
    )


def sbx_openai_credential_available(*, env_file: Path | None = None) -> bool:
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return True
    if env_file is not None:
        sources = _credential_sources(env_file)
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
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
        or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        or os.environ.get("DEEPSEEK_API_KEY", "").strip()
    ):
        return True
    if env_file is not None:
        sources = _credential_sources(env_file)
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
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
        or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        or os.environ.get("DEEPSEEK_API_KEY", "").strip()
    ):
        return False
    if env_file is not None:
        sources = _credential_sources(env_file)
        if _anthropic_secret_value(sources):
            return False
    if not sbx_available():
        return False
    try:
        return SERVICE_ANTHROPIC in list_sbx_secret_services()
    except RuntimeError:
        return False
