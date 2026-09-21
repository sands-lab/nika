"""Capture Python warnings away from the live TTY (Rich Live safe).

Importable from ``nika.cli.main`` before any MCP / FastMCP import so the
pydantic_settings ``lifespan`` warning is buffered instead of printed mid-panel.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

_console = Console()
_lock = threading.Lock()
_counts: dict[str, int] = {}
_installed = False
_original_showwarning: Any = None

_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "mcp",
    "mcp.server",
    "mcp.server.lowlevel",
    "mcp.server.lowlevel.server",
    "mcp.server.streamable_http",
    "mcp.server.streamable_http_manager",
    "uvicorn",
    "uvicorn.access",
    "uvicorn.error",
    "sse_starlette",
    "sse_starlette.sse",
    "nika.run_config.loader",
    "nika.run_config.legacy",
)


def quiet_noisy_loggers() -> None:
    import logging

    root = logging.getLogger()
    if root.level == logging.NOTSET or root.level < logging.WARNING:
        root.setLevel(logging.WARNING)
    for handler in root.handlers:
        if handler.level < logging.WARNING:
            handler.setLevel(logging.WARNING)
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)


def ignore_known_library_warnings() -> None:
    """Spawn-worker path: drop known library warnings (no deferred panel)."""
    import warnings

    try:
        from pydantic_settings.exceptions import (
            IncompleteFieldDefinitionWarning,
        )

        warnings.filterwarnings(
            "ignore", category=IncompleteFieldDefinitionWarning
        )
    except ImportError:
        warnings.filterwarnings(
            "ignore",
            message=".*incomplete definition.*",
            category=UserWarning,
        )


def _format_warning(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
) -> str:
    short_file = filename
    marker = "site-packages/"
    if marker in filename:
        short_file = filename.split(marker, 1)[1]
    text = str(message).strip().splitlines()[0]
    return f"{category.__name__}: {text}  ({short_file}:{lineno})"


def _is_benign_library_warning(
    message: Warning | str, category: type[Warning]
) -> bool:
    """Known third-party noise we never want in the post-run Warnings panel."""
    try:
        from pydantic_settings.exceptions import (
            IncompleteFieldDefinitionWarning,
        )

        if issubclass(category, IncompleteFieldDefinitionWarning):
            return True
    except ImportError:
        pass
    text = str(message)
    return "incomplete definition" in text and "lifespan" in text


def _capture_showwarning(
    message: Warning | str,
    category: type[Warning],
    filename: str,
    lineno: int,
    file: Any = None,
    line: str | None = None,
) -> None:
    if _is_benign_library_warning(message, category):
        return
    key = _format_warning(message, category, filename, lineno)
    with _lock:
        _counts[key] = _counts.get(key, 0) + 1


def install_warning_capture(*, quiet_loggers: bool = True) -> None:
    """Buffer ``warnings.showwarning`` output until :func:`print_deferred_warnings`.

    Set ``quiet_loggers=False`` from the global CLI entrypoint so non-benchmark
    commands keep normal logging; benchmark run enables quieting explicitly.
    """
    global _installed, _original_showwarning
    import warnings

    if quiet_loggers:
        quiet_noisy_loggers()
    ignore_known_library_warnings()
    try:
        from nika.mcp.fastmcp_settings import ensure_fastmcp_settings_ready

        ensure_fastmcp_settings_ready()
    except Exception:  # noqa: BLE001 - MCP may be unavailable in some envs
        pass
    if _installed:
        return
    _original_showwarning = warnings.showwarning
    warnings.showwarning = _capture_showwarning
    _installed = True


def warning_capture_installed() -> bool:
    return _installed


def print_deferred_warnings() -> None:
    """Print buffered warnings in a dedicated post-run panel (if any)."""
    with _lock:
        items = sorted(_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        _counts.clear()
    if not items:
        return
    body = Table.grid(expand=True)
    body.add_column()
    for key, count in items:
        suffix = f"  (×{count})" if count > 1 else ""
        body.add_row(Text(f"• {key}{suffix}"))
    _console.print(
        Panel(
            body,
            title="[bold]Warnings[/bold]",
            title_align="left",
            border_style="yellow",
            expand=True,
        )
    )


def apply_worker_warning_env() -> None:
    """Best-effort env filter for spawn workers (message must match from start)."""
    flag = "ignore:Field:UserWarning"
    existing = os.environ.get("PYTHONWARNINGS", "").strip()
    if flag in existing or "ignore:Field:" in existing:
        return
    os.environ["PYTHONWARNINGS"] = f"{existing},{flag}" if existing else flag
