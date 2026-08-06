"""Remote lab-host configuration from run config YAML."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

ENV_REMOTE_ENABLED = "NIKA_REMOTE_ENABLED"  # legacy (ignored; use YAML)
ENV_REMOTE_URL = "NIKA_REMOTE_URL"
ENV_REMOTE_SERVER = "NIKA_REMOTE_SERVER"
ENV_REMOTE_ARTIFACT_ROOT = "NIKA_REMOTE_ARTIFACT_ROOT"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_truthy(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in _TRUTHY


@dataclass(frozen=True)
class RemoteConfig:
    """Client-side remote dial settings."""

    enabled: bool
    url: str
    artifact_root: str

    @property
    def base_url(self) -> str:
        return self.url.rstrip("/")

    @property
    def host(self) -> str:
        parsed = urlparse(self.url)
        if not parsed.hostname:
            raise ValueError(f"Remote URL must include a hostname: {self.url!r}")
        return parsed.hostname

    def gateway_url(self, port: int) -> str:
        """Build a routable MCP gateway URL for agents on the local host."""
        parsed = urlparse(self.url)
        scheme = parsed.scheme or "http"
        return f"{scheme}://{self.host}:{int(port)}"


def is_remote_server() -> bool:
    """True when this process is the lab-host remote daemon."""
    return _env_truthy(ENV_REMOTE_SERVER, "false")


def is_remote_enabled() -> bool:
    """True when local workflows should forward lab ops to a remote daemon.

    Always false inside ``nika remote serve`` so daemon handlers never recurse.
    """
    if is_remote_server():
        return False
    try:
        from nika.run_config.loader import get_run_config

        remote = get_run_config().nika.remote
    except Exception:
        return False
    if not remote.enabled:
        return False
    return bool((remote.url or "").strip())


def load_remote_config() -> RemoteConfig:
    """Load remote client config from run config YAML."""
    from nika.run_config.loader import get_run_config

    remote = get_run_config().nika.remote
    url = (remote.url or "").strip()
    enabled = is_remote_enabled()
    if enabled and not url:
        raise ValueError(
            "nika.remote.enabled is true but nika.remote.url is empty. "
            "Set url in config/nika.yaml (e.g. http://<lab-host>:8700)."
        )
    return RemoteConfig(
        enabled=enabled,
        url=url or "http://127.0.0.1:8700",
        artifact_root=(remote.artifact_root or "").strip(),
    )
