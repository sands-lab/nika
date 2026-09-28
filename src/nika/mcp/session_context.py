"""Resolve lab and result paths for MCP tools from session binding."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any

from nika.config import RESULTS_DIR
from nika.mcp.gateway.context import get_bound_session_id
from nika.runtime.factory import resolve_backend
from nika.utils.session_store import SessionStore

SESSION_ID_ENV = "NIKA_SESSION_ID"


def require_session_id() -> str:
    """Return the canonical SessionStore session id for the bound agent handle."""
    from nika.mcp.gateway.session_registry import resolve_canonical_session_id

    session_id = get_bound_session_id() or os.getenv(SESSION_ID_ENV)
    if not session_id:
        raise ValueError(
            f"{SESSION_ID_ENV} is not set. MCP tools must be started with a bound session id."
        )
    return resolve_canonical_session_id(session_id)


def get_session_meta() -> dict[str, Any]:
    session_id = require_session_id()
    try:
        meta = SessionStore().get_session(session_id)
    except FileNotFoundError:
        try:
            from nika.workflows.session.close import load_session_meta_for_close

            meta = load_session_meta_for_close(session_id)
        except FileNotFoundError as exc:
            # Host loaders embed the canonical trial id; never echo it to agents.
            raise FileNotFoundError("Session not found.") from exc
    if meta.get("status") != "running":
        raise ValueError("Session is not running.")
    return meta


def _lab_name_from_meta(meta: dict[str, Any]) -> str:
    lab_name = meta.get("lab_name") or (meta.get("scenario_params") or {}).get(
        "lab_name"
    )
    if not lab_name:
        raise ValueError("Session has no lab_name.")
    return str(lab_name)


def get_lab_name() -> str:
    return _lab_name_from_meta(get_session_meta())


def get_session_dir() -> str:
    meta = get_session_meta()
    session_dir = meta.get("session_dir")
    if session_dir:
        return str(session_dir)
    return f"{RESULTS_DIR}/{meta['session_id']}"


# Host APIs per canonical session id. Building one lists the lab's containers
# and networks (Kathara) or runs ``clab inspect`` (Containerlab); doing that on
# every tool call dominated simple exec latency. An entry is reused only while
# the session's backend, lab name, status and creation time are unchanged, so
# a redeployed or restarted session gets a fresh API.
_API_CACHE: dict[tuple[str, str], tuple[tuple[Any, ...], Any]] = {}
_API_CACHE_LOCK = threading.Lock()


def _api_cache_key(meta: dict[str, Any]) -> tuple[Any, ...]:
    return (
        resolve_backend(meta),
        _lab_name_from_meta(meta),
        meta.get("status"),
        meta.get("created_at"),
    )


def _cached_api(kind: str, meta: dict[str, Any], build: Callable[[], Any]) -> Any:
    session_id = str(meta.get("session_id") or require_session_id())
    slot = (session_id, kind)
    key = _api_cache_key(meta)
    with _API_CACHE_LOCK:
        cached = _API_CACHE.get(slot)
        if cached is not None and cached[0] == key:
            return cached[1]
    api = build()
    with _API_CACHE_LOCK:
        _API_CACHE[slot] = (key, api)
    return api


def get_lab_api():
    """Return KatharaBaseAPI or ContainerlabBaseAPI for the current session backend."""
    from nika.service.lab.host_api import create_host_api

    meta = get_session_meta()
    return _cached_api(
        "host",
        meta,
        lambda: create_host_api(
            lab_name=_lab_name_from_meta(meta),
            backend=resolve_backend(meta),
            session_meta=meta,
        ),
    )


def get_lab_runtime():
    """Return the cached ``LabRuntime`` for the current session.

    Reuses the host API's runtime when it has one (containerlab); otherwise
    caches a runtime built from session metadata.
    """
    runtime = getattr(get_lab_api(), "runtime", None)
    if runtime is not None:
        return runtime
    from nika.runtime.factory import runtime_for_session

    meta = get_session_meta()
    return _cached_api("runtime", meta, lambda: runtime_for_session(meta))


def get_srl_api():
    """Return ContainerlabSRLAPI for the current containerlab session."""
    from nika.service.containerlab import ContainerlabSRLAPI

    meta = get_session_meta()
    if resolve_backend(meta) != "containerlab":
        raise ValueError("SRL MCP tools require a containerlab session.")
    host_api = get_lab_api()
    return _cached_api("srl", meta, lambda: ContainerlabSRLAPI(host_api.runtime))
