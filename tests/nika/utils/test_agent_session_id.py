"""Opaque agent session id helpers."""

from __future__ import annotations

from nika.utils.agent_session_id import (
    AGENT_SESSION_TAG,
    make_agent_session_id,
    resolve_agent_session_id,
)


def test_make_agent_session_id_is_opaque_tagged() -> None:
    sid = make_agent_session_id()
    assert f"-{AGENT_SESSION_TAG}-" in sid
    assert "dhcp_missing" not in sid
    assert "__" not in sid


def test_resolve_falls_back_to_session_id_when_missing() -> None:
    assert (
        resolve_agent_session_id({"session_id": "campus_lan__dhcp_missing_subnet__t01"})
        == "campus_lan__dhcp_missing_subnet__t01"
    )


def test_resolve_prefers_agent_session_id() -> None:
    assert (
        resolve_agent_session_id(
            {
                "session_id": "campus_lan__dhcp_missing_subnet__t01",
                "agent_session_id": "20260101-120000-a-abcdef",
            }
        )
        == "20260101-120000-a-abcdef"
    )


def test_resolve_from_object_attrs() -> None:
    class _S:
        session_id = "readable__case"
        agent_session_id = "20260101-120000-a-fedcba"

    assert resolve_agent_session_id(_S()) == "20260101-120000-a-fedcba"
