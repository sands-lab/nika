"""Workflow hook for advancing MCP gateway phase."""

from __future__ import annotations

from agent.protocols import PHASES, SUBMISSION
from nika.mcp.gateway.session_registry import advance_phase as _advance_phase


def advance_mcp_phase(session_id: str, phase: str) -> None:
    """Advance the gateway phase for *session_id* (called between workflow phases)."""
    if phase not in PHASES:
        raise ValueError(f"Invalid MCP phase: {phase!r}")
    _advance_phase(session_id, phase)  # type: ignore[arg-type]


__all__ = ["advance_mcp_phase", "SUBMISSION"]
