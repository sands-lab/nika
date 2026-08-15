"""Reasoning-effort helpers shared by BYO agents."""

from __future__ import annotations

# Anthropic Messages API output_config.effort values (no none/minimal).
_ANTHROPIC_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})


def map_anthropic_effort(reasoning_effort: str | None) -> str | None:
    """Map NIKA reasoning_effort to Anthropic ``output_config.effort``.

    Returns ``None`` when effort should be omitted (unset or ``none``).
    ``minimal`` maps to ``low``.
    """
    if reasoning_effort is None or reasoning_effort == "none":
        return None
    mapped = "low" if reasoning_effort == "minimal" else reasoning_effort
    if mapped not in _ANTHROPIC_EFFORT_LEVELS:
        raise ValueError(
            "Anthropic reasoning_effort must map to one of "
            f"{', '.join(sorted(_ANTHROPIC_EFFORT_LEVELS))} "
            f"(got {reasoning_effort!r})"
        )
    return mapped


def anthropic_output_config(
    reasoning_effort: str | None,
) -> dict[str, dict[str, str]] | None:
    """Return ``{\"output_config\": {\"effort\": ...}}`` or ``None``."""
    effort = map_anthropic_effort(reasoning_effort)
    if effort is None:
        return None
    return {"output_config": {"effort": effort}}
