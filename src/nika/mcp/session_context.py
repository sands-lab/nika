"""Resolve lab and result paths for MCP tools from session binding."""

from __future__ import annotations

import os
from typing import Any

from nika.config import RESULTS_DIR
from nika.mcp.gateway.context import get_bound_session_id
from nika.runtime.factory import resolve_backend
from nika.utils.session_store import SessionStore

SESSION_ID_ENV = "NIKA_SESSION_ID"


def require_session_id() -> str:
    session_id = get_bound_session_id() or os.getenv(SESSION_ID_ENV)
    if not session_id:
        raise ValueError(
            f"{SESSION_ID_ENV} is not set. MCP tools must be started with a bound session id."
        )
    return session_id


def get_session_meta() -> dict[str, Any]:
    session_id = require_session_id()
    try:
        meta = SessionStore().get_session(session_id)
    except FileNotFoundError:
        from nika.workflows.session.close import load_session_meta_for_close

        meta = load_session_meta_for_close(session_id)
    if meta.get("status") != "running":
        raise ValueError(f"Session '{session_id}' is not running.")
    return meta


def _lab_name_from_meta(meta: dict[str, Any]) -> str:
    lab_name = meta.get("lab_name") or (meta.get("scenario_params") or {}).get(
        "lab_name"
    )
    if not lab_name:
        raise ValueError(f"Session '{meta.get('session_id')}' has no lab_name.")
    return str(lab_name)


def get_lab_name() -> str:
    return _lab_name_from_meta(get_session_meta())


def get_session_dir() -> str:
    meta = get_session_meta()
    session_dir = meta.get("session_dir")
    if session_dir:
        return str(session_dir)
    return f"{RESULTS_DIR}/{meta['session_id']}"


def get_lab_api():
    """Return KatharaBaseAPI or ContainerlabBaseAPI for the current session backend."""
    from nika.service.lab.host_api import create_host_api

    meta = get_session_meta()
    return create_host_api(
        lab_name=_lab_name_from_meta(meta),
        backend=resolve_backend(meta),
        session_meta=meta,
    )


def get_srl_api():
    """Return ContainerlabSRLAPI for the current containerlab session."""
    from nika.service.containerlab import ContainerlabSRLAPI
    from nika.service.lab.host_api import create_host_api

    meta = get_session_meta()
    backend = resolve_backend(meta)
    if backend != "containerlab":
        raise ValueError("SRL MCP tools require a containerlab session.")
    host_api = create_host_api(
        lab_name=_lab_name_from_meta(meta),
        backend=backend,
        session_meta=meta,
    )
    return ContainerlabSRLAPI(host_api.runtime)
