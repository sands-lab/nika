"""Dual-key MCP gateway registration for opaque agent session ids."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from nika.mcp.gateway.app import create_gateway_app
from nika.mcp.gateway.context import bind_session, reset_session
from nika.mcp.gateway.session_registry import (
    clear_sessions,
    get_session,
    register_session,
    resolve_canonical_session_id,
    unregister_session,
)
from nika.mcp.session_context import get_session_meta, require_session_id

CASE_KEY = (
    "campus_lan__link_down__s__host_name-backend_web_0__intf_name-eth0__t01"
)
OPAQUE = "20260101-120000-a-abc123"


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


def test_mcp_errors_omit_canonical_case_key(tmp_path: Path, monkeypatch) -> None:
    """Agent-facing MCP/gateway errors must not echo the readable trial id."""
    register_session(
        CASE_KEY,
        agent_session_id=OPAQUE,
        scenario_name="campus_lan",
        session_dir=str(tmp_path / "trials" / CASE_KEY),
    )
    token = bind_session(OPAQUE)
    try:
        with patch(
            "nika.mcp.session_context.SessionStore.get_session",
            return_value={
                "session_id": CASE_KEY,
                "agent_session_id": OPAQUE,
                "status": "finished",
            },
        ):
            with pytest.raises(ValueError, match="Session is not running") as not_running:
                get_session_meta()
        assert CASE_KEY not in str(not_running.value)
    finally:
        reset_session(token)

    clear_sessions()
    client = TestClient(create_gateway_app())
    phase = client.post(
        f"/gateway/sessions/{CASE_KEY}/phase",
        headers={"NIKA-Session-Id": CASE_KEY},
        json={"phase": "submission", "diagnosis_report": "x"},
    )
    assert phase.status_code == 404
    assert phase.json()["error"] == "session not registered"
    assert CASE_KEY not in phase.json()["error"]

    from nika.mcp.servers.common import task_server

    monkeypatch.setattr(
        task_server, "_submission_catalog", lambda: (set(), set(), "report")
    )
    monkeypatch.setattr(
        task_server,
        "get_session_dir",
        lambda: str(tmp_path / "trials" / CASE_KEY),
    )

    def _boom(*_args, **_kwargs):
        raise OSError(f"[Errno 13] Permission denied: '{tmp_path}/trials/{CASE_KEY}'")

    monkeypatch.setattr(task_server.os, "makedirs", _boom)
    submit_result = task_server.submit(is_anomaly=False, root_causes=[])
    assert submit_result == ["Submission failed."]
    assert CASE_KEY not in submit_result[0]


def test_resolve_canonical_falls_back_to_session_store(tmp_path: Path, monkeypatch) -> None:
    """Opaque ids still resolve when the in-process gateway registry misses."""
    from nika.utils.session_store import SessionStore

    sessions_dir = tmp_path / "sessions"
    db_path = tmp_path / "sessions.db"
    store = SessionStore(sessions_dir, db_path)
    store.create_session(
        {
            "session_id": CASE_KEY,
            "agent_session_id": OPAQUE,
            "status": "running",
            "session_dir": str(tmp_path / "trial"),
            "lab_name": "lab-x",
            "scenario_name": "campus_lan",
        }
    )
    monkeypatch.setattr(
        "nika.utils.session_store.SessionStore",
        lambda *args, **kwargs: SessionStore(sessions_dir, db_path),
    )
    clear_sessions()
    assert resolve_canonical_session_id(OPAQUE) == CASE_KEY
    assert store.find_by_agent_session_id(OPAQUE)["session_id"] == CASE_KEY


def test_sibling_gateway_shutdown_keeps_live_managers() -> None:
    """Stopping one gateway must not reset FastMCP state while another is live."""
    from unittest.mock import MagicMock, patch

    from nika.mcp.gateway import lifecycle as life

    life._live_managers.clear()
    life._active_manager = None

    a = MagicMock()
    a.base_url = "http://127.0.0.1:1111"
    a.host = "127.0.0.1"
    a.port = 1111
    a.backend = "kathara"
    b = MagicMock()
    b.base_url = "http://127.0.0.1:2222"
    b.host = "127.0.0.1"
    b.port = 2222
    b.backend = "kathara"

    with life._manager_lock:
        life._live_managers.add(a)
        life._live_managers.add(b)
        life._active_manager = b

    with patch.object(life, "reset_gateway_mcp_state") as reset:
        life._shutdown_manager(a, clear_registry=False)
        reset.assert_not_called()
    assert a not in life._live_managers
    assert b in life._live_managers
    a.stop.assert_called_once()

    with patch.object(life, "reset_gateway_mcp_state") as reset:
        life._shutdown_manager(b, clear_registry=False)
        reset.assert_called_once()
    assert not life._live_managers
