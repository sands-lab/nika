"""Dual-key MCP gateway registration for opaque agent session ids."""

from __future__ import annotations

from nika.mcp.gateway.session_registry import (
    clear_sessions,
    get_session,
    register_session,
    resolve_canonical_session_id,
    unregister_session,
)
from nika.mcp.session_context import require_session_id
from nika.mcp.gateway.context import bind_session, reset_session


def setup_function() -> None:
    clear_sessions()


def teardown_function() -> None:
    clear_sessions()


def test_dual_key_register_resolves_canonical() -> None:
    register_session(
        "campus_lan__dhcp_missing_subnet__m__host_name-dhcp_server__t01",
        agent_session_id="20260101-120000-a-abc123",
        scenario_name="campus_lan",
        session_dir="/tmp/trial",
    )
    opaque = get_session("20260101-120000-a-abc123")
    readable = get_session(
        "campus_lan__dhcp_missing_subnet__m__host_name-dhcp_server__t01"
    )
    assert opaque is not None and readable is not None
    assert opaque is readable
    assert opaque.canonical_session_id == (
        "campus_lan__dhcp_missing_subnet__m__host_name-dhcp_server__t01"
    )
    assert opaque.agent_session_id == "20260101-120000-a-abc123"
    assert (
        resolve_canonical_session_id("20260101-120000-a-abc123")
        == "campus_lan__dhcp_missing_subnet__m__host_name-dhcp_server__t01"
    )


def test_legacy_register_without_agent_id() -> None:
    register_session("legacy-readable-id", scenario_name="dc_clos")
    entry = get_session("legacy-readable-id")
    assert entry is not None
    assert entry.agent_session_id == "legacy-readable-id"
    assert entry.canonical_session_id == "legacy-readable-id"
    assert resolve_canonical_session_id("legacy-readable-id") == "legacy-readable-id"


def test_unregister_clears_both_keys() -> None:
    register_session(
        "canonical-id",
        agent_session_id="opaque-id",
        scenario_name="dc_clos",
    )
    unregister_session("opaque-id")
    assert get_session("opaque-id") is None
    assert get_session("canonical-id") is None


def test_require_session_id_maps_opaque_header_to_canonical() -> None:
    register_session(
        "canonical-trial",
        agent_session_id="opaque-agent",
        scenario_name="dc_clos",
        session_dir="/tmp/x",
    )
    token = bind_session("opaque-agent")
    try:
        assert require_session_id() == "canonical-trial"
    finally:
        reset_session(token)
