"""In-process MCP gateway session phase state."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Literal

from agent.protocols import DIAGNOSIS, SUBMISSION

PolicyMode = Literal["two_phase", "unified"]
Phase = Literal["diagnosis", "submission"]

_lock = Lock()
_sessions: dict[str, "GatewaySession"] = {}


@dataclass
class GatewaySession:
    """Gateway binding for one lab session.

    ``agent_session_id`` is what agents send in ``NIKA-Session-Id`` (opaque when
    set). ``canonical_session_id`` keys SessionStore / trial dirs. Both map to
    the same entry when they differ (dual-key register).
    """

    agent_session_id: str
    canonical_session_id: str
    scenario_name: str
    policy_mode: PolicyMode
    phase: Phase = DIAGNOSIS
    remote_upstreams: dict[str, str] = field(default_factory=dict)
    session_dir: str = ""
    access_policy: dict[str, Any] = field(default_factory=dict)
    node_roles: dict[str, str] = field(default_factory=dict)

    @property
    def session_id(self) -> str:
        """Backward-compatible alias: agent-facing handle."""
        return self.agent_session_id


def register_session(
    session_id: str,
    *,
    agent_session_id: str | None = None,
    scenario_name: str = "",
    policy_mode: PolicyMode = "two_phase",
    remote_upstreams: dict[str, str] | None = None,
    session_dir: str = "",
    access_policy: dict[str, Any] | None = None,
    node_roles: dict[str, str] | None = None,
) -> None:
    """Register under canonical ``session_id`` and optional opaque agent id.

    Lookup works with either key so in-flight agents that still send the
    readable trial id keep working.
    """
    canonical = session_id
    agent_id = (agent_session_id or "").strip() or canonical
    entry = GatewaySession(
        agent_session_id=agent_id,
        canonical_session_id=canonical,
        scenario_name=scenario_name,
        policy_mode=policy_mode,
        phase=DIAGNOSIS,
        remote_upstreams=dict(remote_upstreams or {}),
        session_dir=session_dir,
        access_policy=dict(access_policy or {}),
        node_roles=dict(node_roles or {}),
    )
    with _lock:
        _sessions[agent_id] = entry
        if agent_id != canonical:
            _sessions[canonical] = entry


def set_remote_upstream(session_id: str, server_name: str, base_url: str) -> None:
    with _lock:
        entry = _sessions.get(session_id)
        if entry is None:
            raise KeyError("MCP gateway session not registered")
        entry.remote_upstreams[server_name] = base_url.rstrip("/")


def unregister_session(session_id: str) -> None:
    with _lock:
        entry = _sessions.get(session_id)
        if entry is None:
            _sessions.pop(session_id, None)
            return
        _sessions.pop(entry.agent_session_id, None)
        _sessions.pop(entry.canonical_session_id, None)


def clear_sessions() -> None:
    with _lock:
        _sessions.clear()


def get_session(session_id: str) -> GatewaySession | None:
    with _lock:
        return _sessions.get(session_id)


def resolve_canonical_session_id(session_id: str) -> str:
    """Map an agent or canonical handle to the SessionStore key."""
    entry = get_session(session_id)
    if entry is not None:
        return entry.canonical_session_id
    return session_id


def advance_phase(session_id: str, phase: Phase) -> None:
    with _lock:
        entry = _sessions.get(session_id)
        if entry is None:
            raise KeyError("MCP gateway session not registered")
        if entry.phase == SUBMISSION and phase != SUBMISSION:
            raise ValueError("MCP phase cannot move back from submission")
        if entry.phase == DIAGNOSIS and phase != SUBMISSION:
            raise ValueError("MCP phase must advance from diagnosis to submission")
        entry.phase = phase
