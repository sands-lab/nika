"""Resolve the session directory used by agents inside a sandbox."""

from __future__ import annotations

import os

from agent.sandbox.config import ENV_SANDBOX_EXECUTION, ENV_SESSION_DIR
from agent.sandbox.sbx.agents import ENV_SBX_SANDBOX_NAME


def resolve_agent_session_dir(session_dir: str) -> str:
    """Return the writable session root for agent workspaces.

    Inside an sbx microVM, SDK runners set ``NIKA_SANDBOX_EXECUTION=1``.
    Host-side CLI orchestration sets ``NIKA_SBX_SANDBOX_NAME`` and
    ``NIKA_SESSION_DIR`` to the mounted ``.sandbox_run`` workspace while
    LangGraph stays on the host.
    """
    override = os.environ.get(ENV_SESSION_DIR, "").strip()
    if not override:
        return session_dir
    if os.environ.get(ENV_SANDBOX_EXECUTION, "").strip() == "1":
        return override
    if os.environ.get(ENV_SBX_SANDBOX_NAME, "").strip():
        return override
    return session_dir


def resolve_agent_trace_dir(session_id: str, session_dir: str) -> str:
    """Keep host-orchestrated CLI traces in the result directory."""
    if not os.environ.get(ENV_SBX_SANDBOX_NAME, "").strip():
        return session_dir

    from nika.utils.session_store import SessionStore

    row = SessionStore().find_by_agent_session_id(session_id)
    if row is None or not row.get("session_dir"):
        raise FileNotFoundError(f"Host session for agent {session_id!r} not found")
    return str(row["session_dir"])
