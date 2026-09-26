"""Submission context stays out of reach until the host advances the phase."""

from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest
from starlette.testclient import TestClient

from agent.sandbox.config import ENV_GATEWAY_URL, ENV_SANDBOX_EXECUTION
from agent.sandbox.sbx.agents import ENV_SBX_SANDBOX_NAME
from agent.utils import mcp_client
from nika.mcp.gateway.app import create_gateway_app
from nika.mcp.gateway.phase import phase_advance_token
from nika.mcp.gateway.session_registry import (
    clear_sessions,
    get_session,
    register_session,
)


@pytest.fixture
def registered_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from nika.utils.session_store import SessionStore

    store = SessionStore(tmp_path / "sessions", tmp_path / "sessions.db")
    store.create_session(
        {
            "session_id": "sess-token",
            "scenario_name": "simple_bgp",
            "fault_ontology": ["link_down"],
        }
    )
    monkeypatch.setattr("nika.workflows.agent.submission.SessionStore", lambda: store)
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    clear_sessions()
    register_session(
        "sess-token",
        agent_session_id="opaque-token",
        scenario_name="simple_bgp",
        session_dir=str(session_dir),
    )
    yield session_dir
    clear_sessions()


def test_session_header_alone_cannot_advance_phase(registered_session: Path) -> None:
    """An in-sandbox agent knows its session id but not the host-only token."""
    client = TestClient(create_gateway_app())
    url = "/gateway/sessions/opaque-token/phase"
    body = {"phase": "submission", "diagnosis_report": "pc1 eth0 down"}

    for headers in (
        {"NIKA-Session-Id": "opaque-token"},
        {"NIKA-Session-Id": "opaque-token", "NIKA-Phase-Token": "guess"},
    ):
        response = client.post(url, headers=headers, json=body)
        assert response.status_code == 403
        assert "submission_context" not in response.json()
    assert get_session("opaque-token").phase == "diagnosis"
    assert not (registered_session / "messages.jsonl").exists()

    token = phase_advance_token("sess-token")
    response = client.post(
        url,
        headers={"NIKA-Session-Id": "opaque-token", "NIKA-Phase-Token": token},
        json=body,
    )
    assert response.status_code == 200
    context = response.json()["submission_context"]
    assert context["diagnosis_report"] == "pc1 eth0 down"
    ids = {item["id"] for item in context["fault_ontology"]}
    assert "link_down" in ids
    # Submission catalog is the fixed full fault set (not session-scoped).
    from nika.workflows.agent.submission import fault_candidates

    assert ids == set(fault_candidates())
    assert get_session("opaque-token").phase == "submission"


def test_phase_token_is_reissued_after_reregistration(registered_session: Path) -> None:
    token = phase_advance_token("opaque-token")
    assert phase_advance_token("sess-token") == token
    register_session(
        "sess-token",
        agent_session_id="opaque-token",
        scenario_name="simple_bgp",
        session_dir=str(registered_session),
    )
    assert phase_advance_token("sess-token") != token


def test_begin_submission_refuses_inside_the_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_SANDBOX_EXECUTION, "1")
    monkeypatch.delenv(ENV_SBX_SANDBOX_NAME, raising=False)
    with pytest.raises(RuntimeError, match="runs on the host"):
        mcp_client.begin_submission_mcp_phase("sess-vm", "report")


def _remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_SANDBOX_EXECUTION, raising=False)
    monkeypatch.setattr("nika.remote.config.is_remote_enabled", lambda: True)
    monkeypatch.setenv(ENV_GATEWAY_URL, "http://gateway.test")


def test_remote_advance_sends_phase_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _remote(monkeypatch)
    monkeypatch.setenv(mcp_client.ENV_GATEWAY_PHASE_TOKEN, "host-secret")
    captured: dict = {}

    def fake_urlopen(request: Request, timeout: float = 10):
        captured["headers"] = {k.lower(): v for k, v in request.header_items()}
        captured["body"] = json.loads(request.data.decode("utf-8"))

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(
                    {
                        "submission_context": {
                            "diagnosis_report": "r",
                            "fault_ontology": [],
                            "resources": [],
                        }
                    }
                ).encode()

        return _Resp()

    monkeypatch.setattr(mcp_client.urllib.request, "urlopen", fake_urlopen)
    mcp_client.begin_submission_mcp_phase("sess-remote", "r")
    assert captured["headers"]["nika-phase-token"] == "host-secret"
    assert captured["body"] == {"phase": "submission", "diagnosis_report": "r"}


def test_remote_advance_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _remote(monkeypatch)
    monkeypatch.delenv(mcp_client.ENV_GATEWAY_PHASE_TOKEN, raising=False)
    with pytest.raises(RuntimeError, match=mcp_client.ENV_GATEWAY_PHASE_TOKEN):
        mcp_client.begin_submission_mcp_phase("sess-remote", "r")


def test_remote_advance_surfaces_gateway_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _remote(monkeypatch)
    monkeypatch.setenv(mcp_client.ENV_GATEWAY_PHASE_TOKEN, "host-secret")

    def fake_urlopen(request: Request, timeout: float = 10):
        raise HTTPError(
            request.full_url,
            403,
            "Forbidden",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"error":"NIKA-Phase-Token is missing or invalid"}'),
        )

    monkeypatch.setattr(mcp_client.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="NIKA-Phase-Token"):
        mcp_client.begin_submission_mcp_phase("sess-remote", "report text")
