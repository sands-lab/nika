"""Operator-facing network-change events for session logs."""

from __future__ import annotations

from typing import Any

from nika.utils.logger import log_event

_MESSAGE_CAP = 240


def log_network_change(
    message: str,
    *,
    mechanism: str,
    **data: Any,
) -> None:
    """Emit a structured ``network_change`` event when a lab mechanism applies."""
    text = " ".join(str(message).split())
    if len(text) > _MESSAGE_CAP:
        text = text[: _MESSAGE_CAP - 1] + "…"
    payload = {key: value for key, value in data.items() if value is not None}
    log_event("network_change", text, mechanism=mechanism, **payload)
