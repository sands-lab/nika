"""List runtime session documents."""

from __future__ import annotations

from nika.utils.session_store import SessionStore


def list_sessions(*, running_only: bool = True) -> list[dict]:
    """Return session index rows, newest first."""
    from nika.remote.config import is_remote_enabled

    if is_remote_enabled():
        from nika.remote.workflows import remote_list_sessions

        return remote_list_sessions(running_only=running_only)

    store = SessionStore()
    if running_only:
        return store.list_running_sessions()
    return store.list_all_sessions()
