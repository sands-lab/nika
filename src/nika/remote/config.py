"""Remote lab-host configuration from run config YAML."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlparse

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
    # Lab-host gateway port -> port agents dial (e.g. a local forwarder).
    gateway_ports: dict[int, int] = field(default_factory=dict)
    # Host agents dial for the gateway when it differs from the daemon's host.
    gateway_host: str | None = None
    # The lab is thrown away after the trial: close without undeploying it.
    disposable_lab: bool = False

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
        port = self.gateway_ports.get(int(port), int(port))
        return f"{scheme}://{self.gateway_host or self.host}:{port}"


# One trial at a time per process: benchmark trials run sequentially in-process
# or one per spawn worker. Process-wide (not a ContextVar) so helper threads such
# as the fault-presence watch see the same lab host.
_override: RemoteConfig | None = None
_override_lock = threading.Lock()


@contextmanager
def remote_override(config: RemoteConfig) -> Iterator[RemoteConfig]:
    """Route this process's lab operations to ``config`` instead of the run config."""
    global _override
    with _override_lock:
        if _override is not None:
            raise RuntimeError("A remote override is already active in this process.")
        _override = config
    try:
        yield config
    finally:
        with _override_lock:
            _override = None


def is_remote_server() -> bool:
    """True when this process is the lab-host remote daemon."""
    return _env_truthy(ENV_REMOTE_SERVER, "false")


def event_origin() -> dict[str, str]:
    """Extra field for log rows the lab-host daemon writes, so they can be told apart.

    The daemon's clock can differ from the agent host's (e.g. a lab restored from
    a VM snapshot keeps the snapshot's timeline); see ``remote_clock_offset_sec``.
    """
    return {"origin": "remote"} if is_remote_server() else {}


def is_remote_enabled() -> bool:
    """True when local workflows should forward lab ops to a remote daemon.

    Always false inside ``nika remote serve`` so daemon handlers never recurse.
    """
    if is_remote_server():
        return False
    if _override is not None:
        return True
    try:
        from nika.run_config.loader import get_run_config

        remote = get_run_config().nika.remote
    except Exception:
        return False
    if not remote.enabled:
        return False
    return bool((remote.url or "").strip())


def load_remote_config() -> RemoteConfig:
    """Load remote client config from run config YAML (or the active override)."""
    if _override is not None and not is_remote_server():
        return _override
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
