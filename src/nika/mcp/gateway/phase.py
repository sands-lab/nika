"""Workflow hook for advancing MCP gateway phase."""

from __future__ import annotations

import secrets
from threading import Lock

from agent.protocols import PHASES, SUBMISSION
from nika.mcp.gateway.session_registry import (
    GatewaySession,
    get_session,
)
from nika.mcp.gateway.session_registry import advance_phase as _advance_phase

_token_lock = Lock()
# Keyed by the registered entry so a re-registered session gets a new token.
_phase_tokens: dict[int, tuple[GatewaySession, str]] = {}


def advance_mcp_phase(session_id: str, phase: str) -> None:
    """Advance the gateway phase for *session_id* (called between workflow phases)."""
    if phase not in PHASES:
        raise ValueError(f"Invalid MCP phase: {phase!r}")
    _advance_phase(session_id, phase)  # type: ignore[arg-type]


def phase_advance_token(session_id: str) -> str:
    """Return the host-only secret that authorizes HTTP phase advances.

    Minted on first use for the registered gateway session, in the gateway
    process. Only host orchestration (or a remote attach response) may hand
    it out; it must never be written to a sandbox env, workspace, or MCP config.
    """
    entry = get_session(session_id)
    if entry is None:
        raise KeyError("MCP gateway session not registered")
    with _token_lock:
        known = _phase_tokens.get(id(entry))
        if known is not None and known[0] is entry:
            return known[1]
        # Drop tokens of sessions that were unregistered since.
        for key, (old, _) in list(_phase_tokens.items()):
            if get_session(old.agent_session_id) is not old:
                del _phase_tokens[key]
        token = secrets.token_urlsafe(32)
        _phase_tokens[id(entry)] = (entry, token)
        return token


def phase_advance_token_matches(session_id: str, token: str) -> bool:
    """True when *token* is the minted phase-advance secret for *session_id*."""
    entry = get_session(session_id)
    if entry is None or not token:
        return False
    with _token_lock:
        known = _phase_tokens.get(id(entry))
    if known is None or known[0] is not entry:
        return False
    return secrets.compare_digest(known[1], token)


__all__ = [
    "advance_mcp_phase",
    "phase_advance_token",
    "phase_advance_token_matches",
    "SUBMISSION",
]
