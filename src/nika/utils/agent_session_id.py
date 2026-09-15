"""Opaque agent-facing session handles (anti-shortcut for benchmark case keys).

Host/canonical ``session_id`` stays human-readable for trial dirs and SessionStore.
Agents see ``agent_session_id`` only (env, MCP header, sandbox hostname, workspace path).
"""

from __future__ import annotations

from typing import Any, Mapping

from nika.utils.session_id import make_session_id

AGENT_SESSION_TAG = "a"


def make_agent_session_id() -> str:
    """Mint an opaque agent handle: ``YYYYMMDD-HHMMSS-a-{6hex}``."""
    return make_session_id(session_tag=AGENT_SESSION_TAG)


def resolve_agent_session_id(
    session_or_meta: Any | Mapping[str, Any] | None = None,
    *,
    session_id: str | None = None,
    agent_session_id: str | None = None,
) -> str:
    """Return agent-facing id, falling back to canonical ``session_id`` when absent.

    Accepts a Session-like object, a metadata mapping, or explicit kwargs.
    Legacy / in-flight sessions without ``agent_session_id`` keep working.
    """
    if agent_session_id and str(agent_session_id).strip():
        return str(agent_session_id).strip()

    if session_or_meta is not None:
        if isinstance(session_or_meta, Mapping):
            opaque = session_or_meta.get("agent_session_id")
            if opaque and str(opaque).strip():
                return str(opaque).strip()
            canonical = session_or_meta.get("session_id")
            if canonical and str(canonical).strip():
                return str(canonical).strip()
        else:
            opaque = getattr(session_or_meta, "agent_session_id", None)
            if opaque and str(opaque).strip():
                return str(opaque).strip()
            canonical = getattr(session_or_meta, "session_id", None)
            if canonical and str(canonical).strip():
                return str(canonical).strip()

    if session_id and str(session_id).strip():
        return str(session_id).strip()
    raise ValueError("Cannot resolve agent session id: no session identity provided.")
