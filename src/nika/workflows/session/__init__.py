"""Session lifecycle and inspection (``nika session``)."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "close_session": ("close", "close_session"),
    "wipe_all_containerlab_labs": ("close", "wipe_all_containerlab_labs"),
    "wipe_kathara_labs": ("close", "wipe_kathara_labs"),
    "list_session_containers": ("containers", "list_session_containers"),
    "inspect_session": ("inspect", "inspect_session"),
    "list_sessions": ("list", "list_sessions"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Load session workflows only when their command needs them."""
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute)
    globals()[name] = value
    return value
