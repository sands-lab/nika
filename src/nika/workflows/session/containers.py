"""List lab containers bound to a running session."""

from __future__ import annotations

from typing import Any

from nika.runtime.factory import resolve_backend, runtime_for_session
from nika.service.kathara.docker_utils import list_lab_containers
from nika.utils.session_resolve import resolve_running_session_id
from nika.utils.session_store import SessionStore


def list_session_containers(
    session_id: str | None = None,
    *,
    store: SessionStore | None = None,
) -> tuple[str, str, list[dict[str, Any]]]:
    """Return session id, lab name, and running container rows for the session."""
    from nika.remote.config import is_remote_enabled

    if is_remote_enabled():
        from nika.remote.workflows import remote_list_session_containers

        return remote_list_session_containers(session_id, store=store)

    session_store = store or SessionStore()
    resolved_id = resolve_running_session_id(session_id, store=session_store)
    session = session_store.get_session(resolved_id)
    lab_name = session.get("lab_name")
    if not lab_name:
        raise ValueError(f"Session '{resolved_id}' has no lab_name.")

    backend = resolve_backend(session)
    if backend == "kathara":
        return resolved_id, lab_name, list_lab_containers(lab_name=lab_name)

    runtime = runtime_for_session(session)
    return resolved_id, lab_name, runtime.inspect()
