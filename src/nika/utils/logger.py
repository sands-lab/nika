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

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

_session_dir: str | None = None
_session_events_path: str | None = None
_logger_lock = threading.Lock()


class _JsonlHandler(logging.Handler):
    """Appends a structured JSON line to nika.jsonl."""

    def __init__(self, events_path: str) -> None:
        super().__init__()
        self._path = events_path

    def emit(self, record: logging.LogRecord) -> None:
        entry: dict = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "event": getattr(record, "event_type", "system"),
            "message": record.getMessage(),
        }
        extra = getattr(record, "data", None)
        if extra:
            entry["data"] = extra
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception:
            self.handleError(record)


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("SystemLogger")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _attach_jsonl_handler(events_path: str) -> None:
    logger = logging.getLogger("SystemLogger")
    os.makedirs(os.path.dirname(events_path), exist_ok=True)
    for h in list(logger.handlers):
        if isinstance(h, _JsonlHandler):
            logger.removeHandler(h)
            h.close()
    logger.addHandler(_JsonlHandler(events_path))


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
        if _session_dir and _session_events_path:
            _attach_jsonl_handler(_session_events_path)
        return system_logger


def bind_session_dir(session_dir: str | Path) -> None:
    """Attach per-session nika.jsonl handler; call once session_dir is known.

    Accepts only ``str`` / ``Path``. Mocks that implement ``os.PathLike`` via
    auto ``__fspath__`` are rejected (they resolve to junk CWD paths).
    """
    if not isinstance(session_dir, (str, Path)):
        raise TypeError(
            f"session_dir must be str or Path, got {type(session_dir).__name__}"
        )
    session_dir = str(session_dir)
    global _session_dir, _session_events_path
    with _logger_lock:
        os.makedirs(session_dir, exist_ok=True)
        _session_dir = session_dir
        _session_events_path = os.path.join(session_dir, "nika.jsonl")
        _attach_jsonl_handler(_session_events_path)


def log_event(event_type: str, message: str, **data) -> None:
    """Log a structured event with optional key/value metadata.

    Writes a structured JSON line to nika.jsonl when a session dir is bound.

    Example::
        log_event("env_start", "Lab deployed", scenario="dc_clos", session_id="...")
        log_error_event("failure_inject_error", "Inject failed", error="timeout")
    """
    system_logger.info(message, extra={"event_type": event_type, "data": data or None})


def log_error_event(event_type: str, message: str, **data) -> None:
    """Log a structured ERROR-level event to nika.jsonl when a session dir is bound."""
    system_logger.error(message, extra={"event_type": event_type, "data": data or None})
