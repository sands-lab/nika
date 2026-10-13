"""Launch the local session viewer."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import IO

import uvicorn

from nika.config import RUNTIME_DIR, resolve_results_root
from nika.inspect.server import create_inspect_app

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7580

# Loopback, or all-interfaces. Specific NIC IPs are rejected.
_ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"})

# One background viewer per process (benchmark auto-start).
_background_lock = threading.Lock()
_background_url: str | None = None
_background_server: uvicorn.Server | None = None

# Cross-process singleton: only one foreground `nika inspect` dashboard may
# run at a time. The flock is held for the life of the process, so a crash
# (not just a clean exit) still releases it automatically.
_SINGLETON_LOCK_PATH = RUNTIME_DIR / "inspect.lock"


def validate_bind_host(host: str) -> str:
    """Return a normalized bind host, or raise ``ValueError``."""
    normalized = host.strip()
    key = normalized.lower() if normalized.lower() == "localhost" else normalized
    if key not in _ALLOWED_BIND_HOSTS:
        raise ValueError(
            "nika inspect --host must be 127.0.0.1, localhost, ::1, 0.0.0.0, or :: "
            "(not a specific interface IP). Use 0.0.0.0 to listen on all IPv4 "
            "addresses, then open http://<this-host-ip>:<port>/."
        )
    return "localhost" if key == "localhost" else normalized


def _is_ssh_session() -> bool:
    return bool(os.environ.get("SSH_CLIENT") or os.environ.get("SSH_TTY"))


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


def _should_open_browser(*, open_browser: bool) -> bool:
    # Skip under SSH/WSL: xdg-open often spam Permission denied with no GUI.
    return bool(open_browser) and not _is_ssh_session() and not _is_wsl()


def _try_open_browser(url: str) -> bool:
    """Best-effort browser open; never raise or dump xdg-open noise."""
    try:
        return bool(webbrowser.open(url))
    except (OSError, webbrowser.Error):
        return False


def _port_available(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            try:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except OSError:
                pass
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _pick_port(host: str, port: int) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    if port == 0:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            return int(sock.getsockname()[1])
    if _port_available(host, port):
        return port
    # Fall back to an ephemeral port when the default is busy.
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _browse_url(host: str, port: int) -> str:
    # Browsers treat 0.0.0.0 poorly; always print a loopback URL for local open.
    if host in {"0.0.0.0", "::"}:
        browse_host = "127.0.0.1"
    elif ":" in host:
        browse_host = f"[{host}]"
    else:
        browse_host = host
    return f"http://{browse_host}:{port}/"


def _print_startup(
    *,
    results_root: Path,
    host: str,
    bind_port: int,
    url: str,
    open_browser: bool,
) -> None:
    print("NIKA inspect", file=sys.stderr)
    print(f"  results: {results_root}", file=sys.stderr)
    print(f"  url:     {url}", file=sys.stderr)
    if host in {"0.0.0.0", "::"}:
        print(
            f"  tip:     also http://<this-host-ip>:{bind_port}/ (all interfaces)",
            file=sys.stderr,
        )
    elif _is_ssh_session():
        print(
            f"  tip:     ssh -L {bind_port}:127.0.0.1:{bind_port} <host>",
            file=sys.stderr,
        )
    elif _is_wsl():
        print(
            "  tip:     open the URL in Windows (WSL has no GUI browser here)",
            file=sys.stderr,
        )
    elif _should_open_browser(open_browser=open_browser) and not _try_open_browser(url):
        print(
            "  tip:     open the URL above in a browser (--no-open to skip)",
            file=sys.stderr,
        )


def _read_lock_info() -> dict:
    try:
        raw = _SINGLETON_LOCK_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


def _acquire_singleton_lock() -> IO:
    """Return an open, exclusively-locked handle, or raise ``ValueError``.

    Advisory ``flock`` is released by the OS when the holding process exits
    for any reason (including a crash), so a stale lock file left behind by
    a killed process never blocks a later start.
    """
    _SINGLETON_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(_SINGLETON_LOCK_PATH, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        url = _read_lock_info().get("url")
        detail = f" at {url}" if url else ""
        raise ValueError(
            f"Another nika inspect dashboard is already running{detail}. "
            "Stop it first, or open the link above to use it."
        ) from exc
    return handle


def _write_lock_info(
    handle: IO, *, host: str, port: int, url: str, results_root: Path
) -> None:
    payload = {
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "url": url,
        "results_root": str(results_root),
        "started_at": time.time(),
    }
    handle.seek(0)
    handle.truncate()
    handle.write(json.dumps(payload))
    handle.flush()


def _release_singleton_lock(handle: IO) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    handle.close()


def serve_inspect(
    *,
    result_dir: str | Path | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> None:
    """Start the session viewer (blocking).

    Default bind is loopback. ``0.0.0.0`` / ``::`` listen on all interfaces;
    specific interface IPs are rejected. Only one instance may run at a time;
    a second invocation errors out with the running instance's URL.
    """
    host = validate_bind_host(host)

    results_root = resolve_results_root(result_dir)
    lock_handle = _acquire_singleton_lock()
    try:
        bind_port = _pick_port(host, port)
        app = create_inspect_app(results_root=results_root, bind_host=host)
        url = _browse_url(host, bind_port)
        _write_lock_info(
            lock_handle, host=host, port=bind_port, url=url, results_root=results_root
        )
        _print_startup(
            results_root=results_root,
            host=host,
            bind_port=bind_port,
            url=url,
            open_browser=open_browser,
        )

        uvicorn.run(app, host=host, port=bind_port, log_level="warning")
    finally:
        _release_singleton_lock(lock_handle)


def start_inspect_background(
    *,
    result_dir: str | Path | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> str:
    """Start the session viewer in a daemon thread; return the browse URL.

    Idempotent within a process: a second call returns the existing URL.
    Does not open a browser (callers surface the URL themselves).
    """
    global _background_url, _background_server

    with _background_lock:
        if _background_url is not None:
            return _background_url

        host = validate_bind_host(host)
        results_root = resolve_results_root(result_dir)
        bind_port = _pick_port(host, port)
        app = create_inspect_app(results_root=results_root, bind_host=host)
        url = _browse_url(host, bind_port)

        config = uvicorn.Config(app, host=host, port=bind_port, log_level="warning")
        server = uvicorn.Server(config)
        # Keep Ctrl+C / signals on the owning CLI process (benchmark run).
        server.install_signal_handlers = False

        thread = threading.Thread(
            target=server.run,
            name="nika-inspect-background",
            daemon=True,
        )
        # Publish before wait so a concurrent caller can reuse this attempt.
        _background_server = server
        _background_url = url
        thread.start()

    deadline = time.monotonic() + 5.0
    while not server.started and time.monotonic() < deadline:
        if not thread.is_alive():
            with _background_lock:
                _background_url = None
                _background_server = None
            raise RuntimeError("nika inspect background server exited early")
        time.sleep(0.05)
    if not server.started:
        with _background_lock:
            _background_url = None
            _background_server = None
        raise RuntimeError("nika inspect background server failed to start")

    return url
