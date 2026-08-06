"""Sandbox execution configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ENV_SANDBOX_KEEP = "NIKA_SANDBOX_KEEP"  # legacy name (ignored for ops; use YAML)
ENV_SANDBOX_CPUS = "NIKA_SANDBOX_CPUS"
ENV_SANDBOX_MEMORY = "NIKA_SANDBOX_MEMORY"
ENV_SANDBOX_OFFLINE_SDK_WHEELS = "NIKA_SANDBOX_OFFLINE_SDK_WHEELS"
ENV_SANDBOX_UPSTREAM_PROXY = "NIKA_SANDBOX_UPSTREAM_PROXY"

ENV_SANDBOX_EXECUTION = "NIKA_SANDBOX_EXECUTION"
ENV_SESSION_DIR = "NIKA_SESSION_DIR"
ENV_GATEWAY_URL = "NIKA_MCP_GATEWAY_URL"
ENV_GATEWAY_AGENT_URL = "NIKA_MCP_GATEWAY_AGENT_URL"

SANDBOX_GATEWAY_HOST_BRIDGE = "host.docker.internal"


def _repo_root() -> Path:
    from nika.config import REPO_ROOT

    return REPO_ROOT


def project_credentials_env_file() -> Path:
    """Always the repo-root ``.env`` (shared with NIKA)."""
    return _repo_root() / ".env"


@dataclass(frozen=True)
class SandboxConfig:
    env_file: Path
    keep_container: bool
    cpus: str | None
    memory: str | None
    offline_sdk_wheels: bool


def sandbox_gateway_agent_host(network: str | None = None) -> str:
    """Return the MCP gateway hostname reachable from a Docker Sandbox."""
    _ = network
    return SANDBOX_GATEWAY_HOST_BRIDGE


def load_sandbox_env_values(*paths: Path) -> dict[str, str]:
    """Merge key/value pairs from optional sandbox env files (later paths win)."""
    from dotenv import dotenv_values

    merged: dict[str, str] = {}
    for path in paths:
        if not path.is_file():
            continue
        merged.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    return merged


def resolve_sandbox_config(
    *,
    keep_container: bool | None = None,
    cpus: str | None = None,
    memory: str | None = None,
    offline_sdk_wheels: bool | None = None,
) -> SandboxConfig:
    """Resolve sandbox settings from CLI flags and run config YAML."""
    from nika.run_config.loader import get_run_config

    sbx = get_run_config().nika.sandbox
    return SandboxConfig(
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
    )
