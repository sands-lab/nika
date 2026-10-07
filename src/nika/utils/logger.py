"""System logger: writes structured JSONL events to {session_dir}/nika.jsonl
once a session directory is bound via ``bind_session_dir()``.

Usage
-----
Basic logging (requires a bound session directory):
    from nika.utils.logger import system_logger, bind_session_dir
    bind_session_dir("/path/to/results/20260608-153412-ab3c1f")
    system_logger.info("some message")

Structured event logging:
    from nika.utils.logger import log_event
    log_event("env_start", "Lab deployed", scenario="dc_clos", session_id="...")

Bind a session directory (call once session_dir is known):
    from nika.utils.logger import bind_session_dir
    bind_session_dir("/path/to/results/20260608-153412-ab3c1f")
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from contextvars import ContextVar
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

# Per-context binding: concurrent trials finalized from parent threads (or
# remote API request threads) each write to their own ``nika.jsonl``.
_bound_events_path: ContextVar[str | None] = ContextVar(
    "nika_session_events_path", default=None
)
# Most recent binding in this process; used by threads that never bound a
# session themselves (e.g. helper threads started inside a trial worker).
_default_events_path: str | None = None
_logger_lock = threading.Lock()


def current_events_path() -> str | None:
    """``nika.jsonl`` path events from the current context are written to."""
    return _bound_events_path.get() or _default_events_path


def event_entry(record: logging.LogRecord) -> dict[str, Any]:
    """The ``nika.jsonl`` row for ``record`` (single source for every renderer)."""
    entry: dict[str, Any] = {
        "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
        "level": record.levelname,
        "event": getattr(record, "event_type", "system"),
        "message": record.getMessage(),
    }
    duration_ms = getattr(record, "duration_ms", None)
    if duration_ms is not None:
        entry["duration_ms"] = duration_ms
    extra = getattr(record, "data", None)
    if extra:
        entry["data"] = extra
    return entry


def event_summary(entry: dict[str, Any]) -> str:
    """Human text of a ``nika.jsonl`` row: message, prefixed by WARNING/ERROR."""
    level = str(entry.get("level") or "INFO")
    prefix = f"{level} " if level in {"WARNING", "ERROR", "CRITICAL"} else ""
    return f"{prefix}{entry.get('message') or ''}"


def format_event_line(entry: dict[str, Any]) -> str:
    """One console line for a ``nika.jsonl`` row: ``[event] summary (Ns)``."""
    duration_ms = entry.get("duration_ms")
    suffix = f" ({float(duration_ms) / 1000:.1f}s)" if duration_ms is not None else ""
    return f"[{entry.get('event') or 'system'}] {event_summary(entry)}{suffix}"


class _JsonlHandler(logging.Handler):
    """Appends a structured JSON line to the bound session's nika.jsonl."""

    def emit(self, record: logging.LogRecord) -> None:
        path = current_events_path()
        if not path:
            return
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(event_entry(record), ensure_ascii=False, default=str)
                    + "\n"
                )
        except Exception:
            self.handleError(record)


_console_events = False


class _ConsoleHandler(logging.Handler):
    """Prints each event to stderr exactly as its ``nika.jsonl`` row reads."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            print(format_event_line(event_entry(record)), file=sys.stderr, flush=True)
        except Exception:
            self.handleError(record)


def set_console_events(enabled: bool) -> None:
    """Show events on stderr (interactive CLI) or keep them file-only (batch)."""
    global _console_events
    with _logger_lock:
        _console_events = enabled
        logger = logging.getLogger("SystemLogger")
        for h in [h for h in logger.handlers if isinstance(h, _ConsoleHandler)]:
            logger.removeHandler(h)
        if enabled:
            logger.addHandler(_ConsoleHandler())


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("SystemLogger")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _attach_jsonl_handler() -> None:
    logger = logging.getLogger("SystemLogger")
    for h in list(logger.handlers):
        if isinstance(h, _JsonlHandler):
            logger.removeHandler(h)
            h.close()
    logger.addHandler(_JsonlHandler())


system_logger = _build_logger()


def refresh_logger() -> logging.Logger:
    """Re-attach session file handlers (useful when log files rotate between commands)."""
    global system_logger
    with _logger_lock:
        logger = logging.getLogger("SystemLogger")
        for h in list(logger.handlers):
            logger.removeHandler(h)
            h.close()
        system_logger = _build_logger()
        if current_events_path():
            _attach_jsonl_handler()
        if _console_events:
            logger.addHandler(_ConsoleHandler())
        return system_logger


def bind_session_dir(session_dir: str | Path) -> None:
    """Route this context's events to ``{session_dir}/nika.jsonl``.

    The binding is a ``ContextVar``: another thread binding a different
    session does not redirect this one. It also becomes the process default
    for threads that have not bound a session.

    Accepts only ``str`` / ``Path``. Mocks that implement ``os.PathLike`` via
    auto ``__fspath__`` are rejected (they resolve to junk CWD paths).
    """
    if not isinstance(session_dir, (str, Path)):
        raise TypeError(
            f"session_dir must be str or Path, got {type(session_dir).__name__}"
        )
    session_dir = str(session_dir)
    global _default_events_path
    with _logger_lock:
        os.makedirs(session_dir, exist_ok=True)
        events_path = os.path.join(session_dir, "nika.jsonl")
        _bound_events_path.set(events_path)
        _default_events_path = events_path
        logger = logging.getLogger("SystemLogger")
        if not any(isinstance(h, _JsonlHandler) for h in logger.handlers):
            _attach_jsonl_handler()


def elapsed_ms(started: float) -> float:
    """Milliseconds since ``time.perf_counter()`` value *started*."""
    return round((time.perf_counter() - started) * 1000, 1)


def _split_duration(data: dict[str, Any]) -> tuple[float | None, dict[str, Any]]:
    if "duration_ms" not in data:
        return None, data
    payload = dict(data)
    raw = payload.pop("duration_ms")
    try:
        duration = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        duration = None
    return duration, payload


def log_event(event_type: str, message: str, **data: Any) -> None:
    """Log a structured event with optional key/value metadata.

    Writes a structured JSON line to nika.jsonl when a session dir is bound.
    Pass ``duration_ms`` for a top-level wall-clock field (audit / view).

    Example::
        log_event("env_start", "Lab deployed", scenario="dc_clos", session_id="...")
        log_error_event("failure_inject_error", "Inject failed", error="timeout")
    """
    duration_ms, payload = _split_duration(data)
    system_logger.info(
        message,
        extra={
            "event_type": event_type,
            "data": payload or None,
            "duration_ms": duration_ms,
        },
    )


def log_warning_event(event_type: str, message: str, **data: Any) -> None:
    """Log a structured WARNING-level event to nika.jsonl when a session dir is bound."""
    duration_ms, payload = _split_duration(data)
    system_logger.warning(
        message,
        extra={
            "event_type": event_type,
            "data": payload or None,
            "duration_ms": duration_ms,
        },
    )


def log_error_event(event_type: str, message: str, **data: Any) -> None:
    """Log a structured ERROR-level event to nika.jsonl when a session dir is bound."""
    duration_ms, payload = _split_duration(data)
    system_logger.error(
        message,
        extra={
            "event_type": event_type,
            "data": payload or None,
            "duration_ms": duration_ms,
        },
    )
