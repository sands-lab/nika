"""Launch the local session viewer."""

from __future__ import annotations

import os
import socket
import sys
import webbrowser
from pathlib import Path

import uvicorn

from nika.config import resolve_results_root
from nika.view.server import create_view_app

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7580

# Loopback, or all-interfaces. Specific NIC IPs are rejected.
_ALLOWED_BIND_HOSTS = frozenset(
    {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}
)


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


def serve_view(
    *,
    result_dir: str | Path | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
) -> None:
    """Start the session viewer (blocking).

    Default bind is loopback. ``0.0.0.0`` / ``::`` listen on all interfaces;
    specific interface IPs are rejected.
    """
    host = validate_bind_host(host)

    results_root = resolve_results_root(result_dir)
    bind_port = _pick_port(host, port)
    app = create_view_app(results_root=results_root)
    # Browsers treat 0.0.0.0 poorly; always print a loopback URL for local open.
    if host in {"0.0.0.0", "::"}:
        browse_host = "127.0.0.1"
    elif ":" in host:
        browse_host = f"[{host}]"
    else:
        browse_host = host
    url = f"http://{browse_host}:{bind_port}/"

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
    elif _should_open_browser(open_browser=open_browser) and not _try_open_browser(
        url
    ):
        print(
            "  tip:     open the URL above in a browser (--no-open to skip)",
            file=sys.stderr,
        )

    uvicorn.run(app, host=host, port=bind_port, log_level="warning")
