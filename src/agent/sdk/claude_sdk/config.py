"""Model settings for sdk.claude_sdk."""

from __future__ import annotations

from agent.cli.claude.config import resolve_claude_model


def resolve_claude_sdk_model(model: str | None) -> str:
    """Return *model* or raise when the run has none."""
    return resolve_claude_model(model)
