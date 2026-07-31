"""Resolve lab and result paths for MCP tools from session binding."""

from __future__ import annotations

import os
from typing import Any

from nika.config import RESULTS_DIR
from nika.runtime.factory import resolve_backend
from nika.service.mcp_gateway.context import get_bound_session_id
from nika.utils.session_store import SessionStore

SESSION_ID_ENV = "NIKA_SESSION_ID"


def require_session_id() -> str:
    session_id = get_bound_session_id()
    if not session_id:
        session_id = os.getenv(SESSION_ID_ENV)
    if not session_id:
        raise ValueError(
            f"{SESSION_ID_ENV} is not set. MCP tools must be started with a bound session id."
        )
    return session_id


def get_session_meta() -> dict[str, Any]:
    session_id = require_session_id()
    meta = SessionStore().get_session(session_id)
    if meta.get("status") != "running":
        raise ValueError(f"Session '{session_id}' is not running.")
    return meta


def get_lab_name() -> str:
    meta = get_session_meta()
    lab_name = meta.get("lab_name") or (meta.get("scenario_params") or {}).get(
        "lab_name"
    )
    if not lab_name:
        raise ValueError(f"Session '{meta.get('session_id')}' has no lab_name.")
    return str(lab_name)


def get_session_dir() -> str:
    meta = get_session_meta()
    session_dir = meta.get("session_dir")
    if session_dir:
        return str(session_dir)
    session_id = str(meta["session_id"])
    return f"{RESULTS_DIR}/{session_id}"


def get_lab_api():
    """Return KatharaBaseAPI or ContainerlabBaseAPI for the current session backend."""
    from nika.service.lab.host_api import create_host_api

    meta = get_session_meta()
    return create_host_api(
        lab_name=get_lab_name(),
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
        lab_name=get_lab_name(),
        backend=backend,
        session_meta=meta,
    )
    return ContainerlabSRLAPI(host_api.runtime)
