"""Request-scoped session binding for MCP gateway HTTP handlers."""

from __future__ import annotations

from contextvars import ContextVar, Token

_bound_session_id: ContextVar[str | None] = ContextVar(
    "nika_mcp_bound_session_id", default=None
)


def bind_session(session_id: str) -> Token:
    return _bound_session_id.set(session_id)


def reset_session(token: Token) -> None:
    _bound_session_id.reset(token)


def get_bound_session_id() -> str | None:
    return _bound_session_id.get()
