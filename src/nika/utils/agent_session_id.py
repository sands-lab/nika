"""Opaque session IDs that agents see instead of a trial's session ID.

A benchmark trial's session ID contains its task ID, which names the
environment and the failure, so it stays on the host. The engine gives each
trial an agent session ID with no task information and writes both IDs to the
session record; the frontend shows the agent only the agent session ID.
"""

from __future__ import annotations

from nika.utils.session_id import make_session_id

AGENT_SESSION_TAG = "a"


def make_agent_session_id() -> str:
    """A random agent handle: ``YYYYMMDD-HHMMSS-a-{6hex}``."""
    return make_session_id(session_tag=AGENT_SESSION_TAG)
