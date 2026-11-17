"""In-process MCP gateway session phase state."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Literal

from agent.protocols import DIAGNOSIS

PolicyMode = Literal["two_phase", "unified"]
Phase = Literal["diagnosis", "submission"]

_lock = Lock()
_sessions: dict[str, "GatewaySession"] = {}
# Session IDs registered under an agent session ID instead. Agents never learn
# them, so a request that names one is refused.
_withheld: set[str] = set()


@dataclass
class GatewaySession:
    """One registered session; ``session_id`` keys the lab and trial directory.

    A benchmark engine registers a trial under ``agent_session_id``, the only
    ID its agent sees.
    """

    session_id: str
    scenario_name: str
    policy_mode: PolicyMode
    phase: Phase = DIAGNOSIS
    remote_upstreams: dict[str, str] = field(default_factory=dict)
    # Set only for a benchmark engine's trial gateway: MCP requests must carry
    # ``access_token``, phase advances ``phase_token``, and tool calls are
    # recorded under ``session_dir``.
    session_dir: str | None = None
    access_token: str | None = None
    phase_token: str | None = None
    agent_session_id: str | None = None

    @property
    def key(self) -> str:
        """The ID requests name this session by."""
        return self.agent_session_id or self.session_id


def register_session(
    session_id: str,
    *,
    scenario_name: str = "",
    policy_mode: PolicyMode = "two_phase",
    remote_upstreams: dict[str, str] | None = None,
    session_dir: str | None = None,
    access_token: str | None = None,
    phase_token: str | None = None,
    agent_session_id: str | None = None,
) -> None:
    entry = GatewaySession(
        session_id=session_id,
        scenario_name=scenario_name,
        policy_mode=policy_mode,
        phase=DIAGNOSIS,
        remote_upstreams=dict(remote_upstreams or {}),
        session_dir=session_dir,
        access_token=access_token,
        phase_token=phase_token,
        agent_session_id=agent_session_id or None,
    )
    with _lock:
        _sessions[entry.key] = entry
        if entry.key != session_id:
            _withheld.add(session_id)


def set_remote_upstream(session_id: str, server_name: str, base_url: str) -> None:
    with _lock:
        entry = _sessions.get(session_id)
        if entry is None:
            raise KeyError(f"MCP gateway session not registered: {session_id!r}")
        entry.remote_upstreams[server_name] = base_url.rstrip("/")


def unregister_session(session_id: str) -> None:
    """Remove the session registered as, or for, *session_id*."""
    with _lock:
        _sessions.pop(session_id, None)
        for key, entry in list(_sessions.items()):
            if entry.session_id == session_id:
                del _sessions[key]
        _withheld.discard(session_id)


def clear_sessions() -> None:
    with _lock:
        _sessions.clear()
        _withheld.clear()


def is_withheld(session_id: str) -> bool:
    """True when *session_id* is a session ID its agent must not use."""
    with _lock:
        return session_id in _withheld


def get_session(session_id: str) -> GatewaySession | None:
    with _lock:
        return _sessions.get(session_id)


def get_phase(session_id: str) -> Phase | None:
    entry = get_session(session_id)
    return entry.phase if entry else None


def advance_phase(session_id: str, phase: Phase) -> None:
    with _lock:
        entry = _sessions.get(session_id)
        if entry is None:
            raise KeyError(f"MCP gateway session not registered: {session_id!r}")
        entry.phase = phase
