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
    session_id: str
    scenario_name: str
    policy_mode: PolicyMode
    phase: Phase = DIAGNOSIS
    remote_upstreams: dict[str, str] = field(default_factory=dict)
    session_dir: str = ""
    access_policy: dict[str, Any] = field(default_factory=dict)
    node_roles: dict[str, str] = field(default_factory=dict)


def register_session(
    session_id: str,
    *,
    scenario_name: str = "",
    policy_mode: PolicyMode = "two_phase",
    remote_upstreams: dict[str, str] | None = None,
    session_dir: str = "",
    access_policy: dict[str, Any] | None = None,
    node_roles: dict[str, str] | None = None,
) -> None:
    with _lock:
        _sessions[session_id] = GatewaySession(
            session_id=session_id,
            scenario_name=scenario_name,
            policy_mode=policy_mode,
            phase=DIAGNOSIS,
            remote_upstreams=dict(remote_upstreams or {}),
            session_dir=session_dir,
            access_policy=dict(access_policy or {}),
            node_roles=dict(node_roles or {}),
        )


def set_remote_upstream(session_id: str, server_name: str, base_url: str) -> None:
    with _lock:
        entry = _sessions.get(session_id)
        if entry is None:
            raise KeyError(f"MCP gateway session not registered: {session_id!r}")
        entry.remote_upstreams[server_name] = base_url.rstrip("/")


def unregister_session(session_id: str) -> None:
    with _lock:
        _sessions.pop(session_id, None)


def clear_sessions() -> None:
    with _lock:
        _sessions.clear()


def get_session(session_id: str) -> GatewaySession | None:
    with _lock:
        return _sessions.get(session_id)


def advance_phase(session_id: str, phase: Phase) -> None:
    with _lock:
        entry = _sessions.get(session_id)
        if entry is None:
            raise KeyError(f"MCP gateway session not registered: {session_id!r}")
        if entry.phase == SUBMISSION and phase != SUBMISSION:
            raise ValueError("MCP phase cannot move back from submission")
        if entry.phase == DIAGNOSIS and phase != SUBMISSION:
            raise ValueError("MCP phase must advance from diagnosis to submission")
        entry.phase = phase
