"""Configuration helpers for sdk.codex_sdk."""

from __future__ import annotations

from agent.cli.codex.codex_worker import REASONING_EFFORT_LEVELS


def validate_reasoning_effort(reasoning_effort: str | None) -> str | None:
    if reasoning_effort is None:
        return None
    if reasoning_effort not in REASONING_EFFORT_LEVELS:
        raise ValueError(
            f"reasoning_effort must be one of {REASONING_EFFORT_LEVELS}, got {reasoning_effort!r}"
        )
    return reasoning_effort
