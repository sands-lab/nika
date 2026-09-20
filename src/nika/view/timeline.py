"""Merge agent and NIKA canonical events into one timeline."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from nika.view.adapters import load_agent_events, load_nika_events
from nika.view.models import CanonicalTraceEvent


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Normalize so mixed naive (legacy) and aware (UTC writers) logs can sort.
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def merge_timelines(
    agent_events: list[CanonicalTraceEvent],
    nika_events: list[CanonicalTraceEvent],
) -> list[CanonicalTraceEvent]:
    """Stable time-ordered merge; undated events keep relative source order at end."""

    dated: list[tuple[datetime, int, int, CanonicalTraceEvent]] = []
    undated: list[CanonicalTraceEvent] = []

    for source_rank, events in ((0, nika_events), (1, agent_events)):
        for idx, event in enumerate(events):
            ts = _parse_ts(event.timestamp)
            if ts is None:
                undated.append(event)
            else:
                # Prefer NIKA before agent on exact timestamp ties (injection before tool).
                dated.append((ts, source_rank, idx, event))

    dated.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in dated] + undated


def build_session_timeline(
    session_dir: Path,
    *,
    source: str | None = None,
) -> list[CanonicalTraceEvent]:
    """Load and optionally filter the session timeline.

    ``source`` may be ``agent``, ``nika``, or ``None`` for the merged view.
    """
    agent = load_agent_events(session_dir)
    nika = load_nika_events(session_dir)
    if source == "nika":
        return nika
    if source == "agent":
        return agent
    return merge_timelines(agent, nika)
