"""Map NIKA sandbox proxy settings to Docker Sandboxes daemon env."""

from __future__ import annotations

import fcntl
import logging
import os
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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

    Reads ``nika.sandbox.upstream_proxy`` from run config (CLI flags merge into
    that field via ``merge_cli``).
    """
    upstream = ""
    try:
        from nika.run_config.loader import get_run_config

        upstream = (get_run_config().nika.sandbox.upstream_proxy or "").strip()
    except Exception:
        upstream = ""
    return upstream or None


def _merge_no_proxy(existing: str) -> str:
    parts = [item.strip() for item in existing.split(",") if item.strip()]
    for item in _SBX_NO_PROXY.split(","):
        if item not in parts:
            parts.append(item)
    return ",".join(parts)


def sbx_process_env(
    base: dict[str, str] | None = None,
    *,
    upstream_proxy: str | None = None,
) -> dict[str, str]:
    """Env for host ``sbx`` subprocesses.

    Copies *base* (or ``os.environ``). When an upstream proxy is configured,
    sets ``DOCKER_SANDBOXES_*`` always. Sets ``HTTP_PROXY`` / ``HTTPS_PROXY``
    only when ``HTTPS_PROXY`` is unset so Docker Hub JWKS fetches can use the
    same proxy. Does not assign ``HTTPS_PROXY`` onto the NIKA process.
    """
    env = dict(base if base is not None else os.environ)
    proxy = (upstream_proxy or resolve_sbx_upstream_proxy() or "").strip()
    if not proxy:
        return env
    env[ENV_DOCKER_SANDBOXES_PROXY] = proxy
    env[ENV_DOCKER_SANDBOXES_NO_PROXY] = _SBX_NO_PROXY
    if not env.get("HTTPS_PROXY", "").strip():
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["NO_PROXY"] = _merge_no_proxy(env.get("NO_PROXY", ""))
    return env


def _apply_process_daemon_env(upstream_proxy: str) -> None:
    os.environ[ENV_DOCKER_SANDBOXES_PROXY] = upstream_proxy
    os.environ[ENV_DOCKER_SANDBOXES_NO_PROXY] = _SBX_NO_PROXY


def sbx_daemon_running() -> bool:
    """True when ``sbx daemon status`` reports a running daemon."""
    daemon_socket = (
        Path.home() / ".local/state/sandboxes/sandboxes/sandboxd/sandboxd.sock"
    )
    if daemon_socket.exists():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1)
        try:
            probe.connect(str(daemon_socket))
            return True
        except OSError:
            pass
        finally:
            probe.close()
    try:
        proc = subprocess.run(
            ["sbx", "daemon", "status"],
            env=sbx_process_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
    except subprocess.TimeoutExpired:
        return False
    return "Status: running" in (proc.stdout or "")


def _daemon_proxy_matches(upstream_proxy: str) -> bool:
    """True when the running sandboxd already has the requested proxy env."""
    sock = os.path.expanduser(
        "~/.local/state/sandboxes/sandboxes/sandboxd/sandboxd.sock"
    )
    try:
        proc = subprocess.run(
            ["fuser", sock],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    # fuser may report multiple pids; take the first contiguous pid token.
    pid = ""
    for part in (
        ((proc.stdout or "") + " " + (proc.stderr or "")).replace(":", " ").split()
    ):
        if part.isdigit():
            pid = part
            break
    if not pid:
        return False
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False
    env = {
        key: value
        for item in raw.split(b"\0")
        if b"=" in item
        for key, value in [item.decode("utf-8", "replace").split("=", 1)]
    }
    return env.get(ENV_DOCKER_SANDBOXES_PROXY, "").strip() == upstream_proxy


_applied_proxy: str | None = None
_proxy_config_lock = threading.Lock()


def _proxy_lock_path() -> Path:
    return Path.home() / ".local/state/sandboxes/nika-sbx-proxy.lock"


@contextmanager
def _cross_process_lock() -> Iterator[None]:
    path = _proxy_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def ensure_sbx_proxy_config(upstream_proxy: str | None) -> None:
    """Apply daemon proxy configuration once, serializing parallel trials."""
    with _proxy_config_lock:
        if upstream_proxy and _applied_proxy == upstream_proxy and sbx_daemon_running():
            _apply_process_daemon_env(upstream_proxy)
            return
        with _cross_process_lock():
            _ensure_sbx_proxy_config(upstream_proxy)


def _ensure_sbx_proxy_config(upstream_proxy: str | None) -> None:
    """Start sandboxd with ``DOCKER_SANDBOXES_PROXY`` when the daemon is down.

    Does not stop a running daemon: parallel NIKA processes share one
    sandboxd, and ``sbx daemon stop`` drops sibling ``sbx exec`` sockets.
    Changing ``upstream_proxy`` requires an idle ``sbx daemon stop`` then a
    NIKA run (or a manual start with ``DOCKER_SANDBOXES_PROXY``).
    """
    global _applied_proxy

    if not sbx_available() or not upstream_proxy:
        return

    _apply_process_daemon_env(upstream_proxy)

    if sbx_daemon_running():
        _applied_proxy = upstream_proxy
        if not _daemon_proxy_matches(upstream_proxy):
            logger.warning(
                "sbx daemon is already running; not restarting it for %s=%s. "
                "Stop the daemon when no sandboxes are running to pick up a "
                "new proxy:\n"
                "  sbx daemon stop\n"
                "  DOCKER_SANDBOXES_PROXY=%s sbx daemon start",
                ENV_DOCKER_SANDBOXES_PROXY,
                upstream_proxy,
                upstream_proxy,
            )
        return

    env = sbx_process_env(upstream_proxy=upstream_proxy)
    subprocess.Popen(
        ["sbx", "daemon", "start"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(60):
        time.sleep(0.5)
        if sbx_daemon_running():
            _applied_proxy = upstream_proxy
            return

    logger.warning(
        "Failed to start sbx daemon with %s=%s. Start it manually:\n"
        "  DOCKER_SANDBOXES_PROXY=%s sbx daemon start",
        ENV_DOCKER_SANDBOXES_PROXY,
        upstream_proxy,
        upstream_proxy,
    )
