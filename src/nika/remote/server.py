"""Run the NIKA Remote lab-host daemon."""

from __future__ import annotations

import logging
import os

import uvicorn

from nika.remote.api import create_remote_app
from nika.remote.config import ENV_REMOTE_SERVER

_CONSOLE_MARKER = "_nika_remote_console"


def configure_host_logging() -> None:
    """Show workflow + remote-op logs on the daemon console (not only events.jsonl)."""
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [remote] %(message)s",
        datefmt="%H:%M:%S",
    )

    def _attach(logger: logging.Logger, *, propagate: bool | None = None) -> None:
        if any(getattr(h, _CONSOLE_MARKER, False) for h in logger.handlers):
            return
        handler = logging.StreamHandler()
        handler.setFormatter(fmt)
        setattr(handler, _CONSOLE_MARKER, True)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        if propagate is not None:
            logger.propagate = propagate

    # Workflows write via SystemLogger with propagate=False; attach a console sink.
    _attach(logging.getLogger("SystemLogger"), propagate=False)
    # Handler-level op logs (env start / inject / MCP / close).
    _attach(logging.getLogger("nika.remote"), propagate=False)


def serve_remote(
    *,
    host: str = "0.0.0.0",
    port: int = 8700,
) -> None:
    """Start the remote control-plane HTTP server (blocking)."""
    os.environ[ENV_REMOTE_SERVER] = "1"
    configure_host_logging()
    app = create_remote_app()
    logging.getLogger("nika.remote").info(
        "daemon listening on http://%s:%s", host, port
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
