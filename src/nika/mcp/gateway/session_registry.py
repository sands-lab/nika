"""In-process MCP gateway session phase state."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Literal

from agent.protocols import DIAGNOSIS, SUBMISSION

Phase = Literal["diagnosis", "submission"]

_lock = Lock()
_sessions: dict[str, "GatewaySession"] = {}
# MCP transport session id (``mcp-session-id``) -> canonical NIKA session id.
# FastMCP runs a transport session's tools in the context of its initialize
# request, so later requests must keep the NIKA session they initialized with.
_transport_owners: dict[str, str] = {}


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
    phase: Phase = DIAGNOSIS
    session_dir: str = ""
    access_policy: dict[str, Any] = field(default_factory=dict)
    node_roles: dict[str, str] = field(default_factory=dict)
    # Diagnosis servers selected for the scenario; ``None`` allows every
    # mounted diagnosis server.
    diagnosis_servers: frozenset[str] | None = None

    @property
    def session_id(self) -> str:
        """Backward-compatible alias: agent-facing handle."""
        return self.agent_session_id


def register_session(
    session_id: str,
    *,
    agent_session_id: str | None = None,
    scenario_name: str = "",
    session_dir: str = "",
    access_policy: dict[str, Any] | None = None,
    node_roles: dict[str, str] | None = None,
    diagnosis_servers: list[str] | None = None,
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
        phase=DIAGNOSIS,
        session_dir=session_dir,
        access_policy=dict(access_policy or {}),
        node_roles=dict(node_roles or {}),
        diagnosis_servers=(
            None if diagnosis_servers is None else frozenset(diagnosis_servers)
        ),
    )
    with _lock:
        _sessions[agent_id] = entry
        if agent_id != canonical:
            _sessions[canonical] = entry


def unregister_session(session_id: str) -> None:
    with _lock:
        entry = _sessions.get(session_id)
        if entry is None:
            _sessions.pop(session_id, None)
            return
        _sessions.pop(entry.agent_session_id, None)
        _sessions.pop(entry.canonical_session_id, None)
        for transport_id, owner in list(_transport_owners.items()):
            if owner == entry.canonical_session_id:
                del _transport_owners[transport_id]


def clear_sessions() -> None:
    with _lock:
        _sessions.clear()
        _transport_owners.clear()


def bind_transport_session(transport_id: str, session_id: str) -> None:
    """Record which NIKA session initialized MCP transport *transport_id*."""
    with _lock:
        entry = _sessions.get(session_id)
        if entry is not None:
            _transport_owners.setdefault(transport_id, entry.canonical_session_id)


def transport_session_matches(transport_id: str, session_id: str) -> bool:
    """Return whether *session_id* may use MCP transport *transport_id*."""
    with _lock:
        owner = _transport_owners.get(transport_id)
        if owner is None:
            return True
        entry = _sessions.get(session_id)
        return entry is not None and entry.canonical_session_id == owner


def get_session(session_id: str) -> GatewaySession | None:
    with _lock:
        return _sessions.get(session_id)


def resolve_canonical_session_id(session_id: str) -> str:
    """Map an agent or canonical handle to the SessionStore key."""
    entry = get_session(session_id)
    if entry is not None:
        return entry.canonical_session_id
    # Registry miss (wrong process, race, or host-side tool path): fall back to
    # SessionStore so parallel sessions are not looked up under the opaque id.
    try:
        from nika.utils.session_store import SessionStore

        store = SessionStore()
        try:
            store.get_session(session_id)
            return session_id
        except FileNotFoundError:
            meta = store.find_by_agent_session_id(session_id)
            if meta and meta.get("session_id"):
                return str(meta["session_id"])
    except Exception:  # noqa: BLE001 — best-effort isolation fallback
        pass
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
