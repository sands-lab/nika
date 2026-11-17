"""A benchmark engine's trial gateway: tokens, phase advance, and records.

Sessions registered without tokens keep release 0.1.0's gateway behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from nika.service.mcp_gateway import app as gateway_app
from nika.service.mcp_gateway.app import create_gateway_app
from nika.service.mcp_gateway.middleware import PhaseGateMiddleware
from nika.service.mcp_gateway.session_registry import (
    clear_sessions,
    get_phase,
    register_session,
)

SERVER = "kathara_base_mcp_server"
CALL = {
    "jsonrpc": "2.0",
    "id": 7,
    "method": "tools/call",
    "params": {"name": "get_reachability", "arguments": {"host": "pc_0_0"}},
}
RESULT = {
    "jsonrpc": "2.0",
    "id": 7,
    "result": {"content": [{"type": "text", "text": "x" * 20000}], "isError": False},
}


@pytest.fixture(autouse=True)
def _sessions():
    clear_sessions()
    yield
    clear_sessions()


def _engine_session(tmp_path: Path) -> None:
    register_session(
        "trial-1",
        scenario_name="dc_clos_bgp",
        session_dir=str(tmp_path),
        access_token="session-secret",
        phase_token="phase-secret",
    )


async def _mcp_server(scope, receive, send):
    """Answers every request with a tool result as a server-sent event."""
    while (await receive()).get("more_body"):
        pass
    body = f"event: message\ndata: {json.dumps(RESULT)}\n\n".encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _mcp_client() -> TestClient:
    gate = PhaseGateMiddleware(_mcp_server, server_name=SERVER, blocked_app=_mcp_server)
    return TestClient(Starlette(routes=[Mount(f"/mcp/{SERVER}", app=gate)]))


def test_engine_session_requires_its_token(tmp_path) -> None:
    _engine_session(tmp_path)
    client = _mcp_client()
    missing = client.post(
        f"/mcp/{SERVER}/mcp", json=CALL, headers={"X-Session-Id": "trial-1"}
    )
    assert missing.status_code == 401
    wrong = client.post(
        f"/mcp/{SERVER}/mcp",
        json=CALL,
        headers={"X-Session-Id": "trial-1", "X-Session-Token": "other"},
    )
    assert wrong.status_code == 401


def test_engine_session_records_tool_calls_unchanged(tmp_path) -> None:
    _engine_session(tmp_path)
    response = _mcp_client().post(
        f"/mcp/{SERVER}/mcp",
        json=CALL,
        headers={"X-Session-Id": "trial-1", "X-Session-Token": "session-secret"},
    )
    assert response.status_code == 200
    assert json.dumps(RESULT) in response.text
    rows = [
        json.loads(line)
        for line in (tmp_path / "tool_calls.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 1
    row = rows[0]
    assert (row["phase"], row["server"], row["tool"], row["call_id"]) == (
        "diagnosis",
        SERVER,
        "get_reachability",
        7,
    )
    assert row["arguments"] == {"host": "pc_0_0"}
    assert row["is_error"] is False
    # Release 0.1.0 does not limit tool output.
    assert row["output"] == row["raw_output"] == "x" * 20000


def test_non_tool_requests_are_not_recorded(tmp_path) -> None:
    _engine_session(tmp_path)
    _mcp_client().post(
        f"/mcp/{SERVER}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"X-Session-Id": "trial-1", "X-Session-Token": "session-secret"},
    )
    assert not (tmp_path / "tool_calls.jsonl").exists()


def test_release_session_needs_no_token_and_is_not_recorded(tmp_path) -> None:
    register_session("sess-1", scenario_name="dc_clos_bgp")
    response = _mcp_client().post(
        f"/mcp/{SERVER}/mcp", json=CALL, headers={"NIKA-Session-Id": "sess-1"}
    )
    assert response.status_code == 200
    assert not list(tmp_path.iterdir())


def test_release_session_phase_advance_is_unchanged() -> None:
    register_session("sess-1", scenario_name="dc_clos_bgp")
    response = TestClient(create_gateway_app()).post(
        "/gateway/sessions/sess-1/phase",
        headers={"NIKA-Session-Id": "sess-1"},
        json={"phase": "submission"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "phase": "submission"}


def test_engine_phase_advance_needs_the_phase_token(tmp_path) -> None:
    _engine_session(tmp_path)
    response = TestClient(create_gateway_app()).post(
        "/gateway/sessions/trial-1/phase",
        headers={"X-Session-Id": "trial-1"},
        json={"phase": "submission", "diagnosis_report": "r"},
    )
    assert response.status_code == 403
    assert get_phase("trial-1") == "diagnosis"


def test_engine_phase_advance_freezes_report_and_returns_context(
    tmp_path, monkeypatch
) -> None:
    _engine_session(tmp_path)
    monkeypatch.setattr(
        gateway_app,
        "_submission_catalogs",
        lambda sid: (["link_down"], [{"id": "node/pc_0_0", "kind": "node"}]),
    )
    client = TestClient(create_gateway_app())
    headers = {"X-Session-Id": "trial-1", "NIKA-Phase-Token": "phase-secret"}
    response = client.post(
        "/gateway/sessions/trial-1/phase",
        headers=headers,
        json={"phase": "submission", "diagnosis_report": "first report"},
    )
    assert response.status_code == 200
    assert response.json()["submission_context"] == {
        "diagnosis_report": "first report",
        "fault_ontology": ["link_down"],
        "resources": [{"id": "node/pc_0_0", "kind": "node"}],
    }
    assert get_phase("trial-1") == "submission"
    # The first report stays frozen.
    again = client.post(
        "/gateway/sessions/trial-1/phase",
        headers=headers,
        json={"phase": "submission", "diagnosis_report": "second report"},
    )
    assert again.json()["submission_context"]["diagnosis_report"] == "first report"
    events = [
        json.loads(line)
        for line in (tmp_path / "messages.jsonl").read_text().splitlines()
    ]
    assert [e["event"] for e in events] == ["diagnosis_frozen"]


def test_engine_phase_advance_without_a_report_is_refused(tmp_path) -> None:
    _engine_session(tmp_path)
    response = TestClient(create_gateway_app()).post(
        "/gateway/sessions/trial-1/phase",
        headers={"X-Session-Id": "trial-1", "NIKA-Phase-Token": "phase-secret"},
        json={"phase": "submission"},
    )
    assert response.status_code == 400
    assert get_phase("trial-1") == "diagnosis"


def test_tool_result_keeps_unicode_line_separators() -> None:
    from nika.service.mcp_gateway.tool_log import tool_result

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": "a b"}], "isError": False},
    }
    body = f"event: message\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    assert tool_result(body.encode(), "text/event-stream") == (False, "a b")


TRIAL_ID = "dc_clos_bgp__host_crash__45fc915a__t01__r6adaabfd"
AGENT_ID = "20261117-130215-a-9c41e7"


def _agent_session(tmp_path: Path) -> None:
    register_session(
        TRIAL_ID,
        scenario_name="dc_clos_bgp",
        session_dir=str(tmp_path),
        access_token="session-secret",
        phase_token="phase-secret",
        agent_session_id=AGENT_ID,
    )


def test_agent_session_id_reaches_the_trial(tmp_path) -> None:
    from nika.service.mcp_gateway.context import get_bound_session_id

    _agent_session(tmp_path)
    bound: list[str | None] = []

    async def tool_server(scope, receive, send):
        bound.append(get_bound_session_id())
        await _mcp_server(scope, receive, send)

    gate = PhaseGateMiddleware(tool_server, server_name=SERVER, blocked_app=tool_server)
    client = TestClient(Starlette(routes=[Mount(f"/mcp/{SERVER}", app=gate)]))
    response = client.post(
        f"/mcp/{SERVER}/mcp",
        json=CALL,
        headers={"X-Session-Id": AGENT_ID, "X-Session-Token": "session-secret"},
    )
    assert response.status_code == 200
    # Tools address the lab by the trial's session ID; the record lands in its
    # trial directory.
    assert bound == [TRIAL_ID]
    assert (tmp_path / "tool_calls.jsonl").is_file()


def test_trial_session_id_is_refused(tmp_path) -> None:
    _agent_session(tmp_path)
    response = _mcp_client().post(
        f"/mcp/{SERVER}/mcp",
        json=CALL,
        headers={"X-Session-Id": TRIAL_ID, "X-Session-Token": "session-secret"},
    )
    assert response.status_code == 403
    assert TRIAL_ID not in response.text
    assert not (tmp_path / "tool_calls.jsonl").exists()


def test_phase_advance_names_the_agent_session(tmp_path, monkeypatch) -> None:
    _agent_session(tmp_path)
    monkeypatch.setattr(
        gateway_app, "_submission_catalogs", lambda sid: (["host_crash"], [])
    )
    client = TestClient(create_gateway_app())
    headers = {"NIKA-Phase-Token": "phase-secret"}
    refused = client.post(
        f"/gateway/sessions/{TRIAL_ID}/phase",
        headers={**headers, "X-Session-Id": TRIAL_ID},
        json={"phase": "submission", "diagnosis_report": "r"},
    )
    assert refused.status_code == 404
    accepted = client.post(
        f"/gateway/sessions/{AGENT_ID}/phase",
        headers={**headers, "X-Session-Id": AGENT_ID},
        json={"phase": "submission", "diagnosis_report": "r"},
    )
    assert accepted.status_code == 200
    assert get_phase(AGENT_ID) == "submission"
    assert TRIAL_ID not in accepted.text


def test_unregistering_the_trial_removes_its_agent_session(tmp_path) -> None:
    from nika.service.mcp_gateway.session_registry import (
        get_session,
        is_withheld,
        unregister_session,
    )

    _agent_session(tmp_path)
    unregister_session(TRIAL_ID)
    assert get_session(AGENT_ID) is None
    assert not is_withheld(TRIAL_ID)
