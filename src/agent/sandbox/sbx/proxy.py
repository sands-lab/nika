"""Map NIKA sandbox proxy settings to Docker Sandboxes daemon env."""

from __future__ import annotations

import logging
import os
import subprocess
import time

from agent.sandbox.config import (
    ENV_SANDBOX_UPSTREAM_PROXY,
    load_sandbox_env_values,
)
from agent.sandbox.sbx.client import sbx_available

logger = logging.getLogger(__name__)

# Keep MCP gateway and loopback off the upstream proxy.
_SBX_NO_PROXY = "host.docker.internal,localhost,127.0.0.1"
ENV_DOCKER_SANDBOXES_PROXY = "DOCKER_SANDBOXES_PROXY"
ENV_DOCKER_SANDBOXES_NO_PROXY = "DOCKER_SANDBOXES_NO_PROXY"


def resolve_sbx_upstream_proxy(
    *,
    env_file: os.PathLike[str] | None = None,
) -> str | None:
    """Optional upstream proxy URL for the sbx daemon (off by default).

    Prefer ``nika.sandbox.upstream_proxy`` in run config, then process env /
    ``--sandbox-proxy`` for one-shot overrides.
    """
    upstream = os.environ.get(ENV_SANDBOX_UPSTREAM_PROXY, "").strip()
    if not upstream:
        try:
            from nika.run_config.loader import get_run_config

            upstream = (get_run_config().nika.sandbox.upstream_proxy or "").strip()
        except Exception:
            upstream = ""
    if not upstream and env_file is not None:
        upstream = (
            load_sandbox_env_values(env_file)
            .get(ENV_SANDBOX_UPSTREAM_PROXY, "")
            .strip()
        )
    return upstream or None


def _daemon_running() -> bool:
    proc = subprocess.run(
        ["sbx", "daemon", "status"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return "Status: running" in (proc.stdout or "")


_applied_proxy: str | None = None


def ensure_sbx_proxy_config(upstream_proxy: str | None) -> None:
    """Reload the sbx daemon with ``DOCKER_SANDBOXES_PROXY`` when configured.

    Uses stop + background start as the current user (``sbx`` has no
    ``daemon restart``, and sudo would bind the wrong state directory).
    Skips reload when the same proxy was already applied and the daemon is up.
    """
    global _applied_proxy

    if not sbx_available() or not upstream_proxy:
        return
    if _applied_proxy == upstream_proxy and _daemon_running():
        return

    env = os.environ.copy()
    env[ENV_DOCKER_SANDBOXES_PROXY] = upstream_proxy
    env[ENV_DOCKER_SANDBOXES_NO_PROXY] = _SBX_NO_PROXY

    subprocess.run(
        ["sbx", "daemon", "stop"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    subprocess.Popen(
        ["sbx", "daemon", "start"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(40):
        time.sleep(0.25)
        if _daemon_running():
            _applied_proxy = upstream_proxy
            return

    logger.warning(
        "Failed to reload sbx daemon with %s=%s. Start it manually:\n"
        "  DOCKER_SANDBOXES_PROXY=%s sbx daemon stop\n"
        "  DOCKER_SANDBOXES_PROXY=%s sbx daemon start",
        ENV_DOCKER_SANDBOXES_PROXY,
        upstream_proxy,
        upstream_proxy,
        upstream_proxy,
    )
