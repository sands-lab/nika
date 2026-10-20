"""Sandbox execution configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_SANDBOX_CPUS = "NIKA_SANDBOX_CPUS"
ENV_SANDBOX_MEMORY = "NIKA_SANDBOX_MEMORY"
ENV_SANDBOX_OFFLINE_SDK_WHEELS = "NIKA_SANDBOX_OFFLINE_SDK_WHEELS"
ENV_SANDBOX_UPSTREAM_PROXY = "NIKA_SANDBOX_UPSTREAM_PROXY"

ENV_GATEWAY_URL = "NIKA_MCP_GATEWAY_URL"
ENV_GATEWAY_AGENT_URL = "NIKA_MCP_GATEWAY_AGENT_URL"
# Access token of a remote gateway's session (a local gateway keeps its own).
ENV_GATEWAY_SESSION_TOKEN = "NIKA_MCP_GATEWAY_SESSION_TOKEN"

SANDBOX_GATEWAY_HOST_BRIDGE = "host.docker.internal"


def _repo_root() -> Path:
    from nika.config import REPO_ROOT

    return REPO_ROOT


def project_credentials_env_file() -> Path:
    """Always the repo-root ``.env`` (shared with NIKA)."""
    return _repo_root() / ".env"


@dataclass(frozen=True)
class SandboxConfig:
    backend: str
    env_file: Path
    keep_container: bool
    cpus: int | None
    memory: str | None
    offline_sdk_wheels: bool
    # Only for e2b: service URL (None: E2B SDK defaults).
    url: str | None = None


def load_sandbox_env_values(*paths: Path) -> dict[str, str]:
    """Merge key/value pairs from optional sandbox env files (later paths win)."""
    from dotenv import dotenv_values

    merged: dict[str, str] = {}
    for path in paths:
        if not path.is_file():
            continue
        merged.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    return merged


def load_credential_sources(
    env_file: Path, *, provider: str, agent_type: str
) -> dict[str, str]:
    """Load .env then keep only keys needed for the active provider mapping.

    Live process env overrides ``.env`` for the known credential keys. The
    result holds real secrets: use it on the host only.
    """
    from nika.agents.provider_env import (
        ENV_ANTHROPIC_API_KEY,
        ENV_ANTHROPIC_AUTH_TOKEN,
        ENV_ANTHROPIC_BASE_URL,
        ENV_CUSTOM_API_KEY,
        ENV_CUSTOM_BASE_URL,
        ENV_DEEPSEEK_API_KEY,
        ENV_OPENAI_API_KEY,
        map_provider_credentials,
    )

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
    ):
        value = os.environ.get(key, "").strip()
        if value:
            merged[key] = value

    if not provider:
        return merged

    mapped = map_provider_credentials(
        agent_type=agent_type, provider=provider, sources=merged
    )
    # Keep mapped credentials and non-secret custom endpoint fields.
    allowed = set(mapped) | {
        ENV_CUSTOM_BASE_URL,
        ENV_CUSTOM_API_KEY,
        "NIKA_CUSTOM_MODEL",
    }
    return {k: v for k, v in {**merged, **mapped}.items() if k in allowed and v}


def resolve_sandbox_config(
    *,
    keep_container: bool | None = None,
    cpus: int | None = None,
    memory: str | None = None,
    offline_sdk_wheels: bool | None = None,
) -> SandboxConfig:
    """Resolve sandbox settings from CLI flags and run config YAML."""
    from nika.run_config.loader import get_run_config

    sbx = get_run_config().nika.sandbox
    return SandboxConfig(
        backend=sbx.backend,
        env_file=project_credentials_env_file(),
        keep_container=(
            keep_container if keep_container is not None else bool(sbx.keep)
        ),
        cpus=cpus if cpus is not None else sbx.cpus,
        memory=memory if memory is not None else sbx.memory,
        offline_sdk_wheels=(
            offline_sdk_wheels
            if offline_sdk_wheels is not None
            else bool(sbx.offline_sdk_wheels)
        ),
        url=sbx.url,
    )
