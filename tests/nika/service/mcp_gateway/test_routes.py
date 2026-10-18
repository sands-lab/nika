from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from nika.mcp.gateway.app import create_gateway_app
from nika.mcp.gateway.phase import phase_advance_token
from nika.mcp.gateway.session_registry import (
    clear_sessions,
    get_session,
    register_session,
)
from nika.utils.session_store import SessionStore
from nika.workflows.agent.submission import fault_candidates


def _phase_headers() -> dict[str, str]:
    return {
        "NIKA-Session-Id": "sess-1",
        "NIKA-Phase-Token": phase_advance_token("sess-1"),
    }


pytestmark = pytest.mark.unit


class GatewayPhaseRouteTest:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path, monkeypatch) -> None:
        store = SessionStore(tmp_path / "sessions", tmp_path / "sessions.db")
        store.create_session(
            {
                "session_id": "sess-1",
                "scenario_name": "simple_bgp",
            }
        )
        monkeypatch.setattr(
            "nika.workflows.agent.submission.SessionStore", lambda: store
        )
        clear_sessions()
        yield
        clear_sessions()

    def test_advance_phase_freezes_diagnosis_report(self, tmp_path: Path) -> None:
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        register_session(
            "sess-1",
            scenario_name="simple_bgp",
            session_dir=str(session_dir),
        )
        client = TestClient(create_gateway_app())
        response = client.post(
            "/gateway/sessions/sess-1/phase",
            headers=_phase_headers(),
            json={"phase": "submission", "diagnosis_report": "link down on pc1"},
        )
        assert response.status_code == 200
        assert response.json()["phase"] == "submission"
        context = response.json()["submission_context"]
        # Every launch mode offers the same fixed candidate set.
        assert [item["id"] for item in context["fault_ontology"]] == fault_candidates()
        assert "node/pc1" in {item["id"] for item in context["resources"]}

        messages = (session_dir / "messages.jsonl").read_text(encoding="utf-8")
        event = json.loads(messages.strip().splitlines()[-1])
        assert event["event"] == "diagnosis_frozen"
        assert event["report"] == "link down on pc1"
        assert context["diagnosis_report"] == event["report"]

        response = client.post(
            "/gateway/sessions/sess-1/phase",
            headers=_phase_headers(),
            json={"phase": "diagnosis"},
        )
        assert response.status_code == 409
        assert "submission_context" not in response.json()
        assert get_session("sess-1").phase == "submission"

    def test_advance_phase_rejects_submission_without_freeze(
        self, tmp_path: Path
    ) -> None:
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        register_session(
            "sess-1",
            scenario_name="simple_bgp",
            session_dir=str(session_dir),
        )
        client = TestClient(create_gateway_app())
        response = client.post(
            "/gateway/sessions/sess-1/phase",
            headers=_phase_headers(),
            json={"phase": "submission"},
        )
        assert response.status_code == 400
        assert "diagnosis_report" in response.json()["error"]
        assert "submission_context" not in response.json()
        assert get_session("sess-1").phase == "diagnosis"

    def test_advance_phase_allows_idempotent_advance_after_freeze(
        self, tmp_path: Path
    ) -> None:
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        (session_dir / "messages.jsonl").write_text(
            json.dumps(
                {
                    "phase": "diagnosis",
                    "event": "diagnosis_frozen",
                    "report": "already frozen",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        register_session(
            "sess-1",
            scenario_name="simple_bgp",
            session_dir=str(session_dir),
        )
        client = TestClient(create_gateway_app())
        response = client.post(
            "/gateway/sessions/sess-1/phase",
            headers=_phase_headers(),
            json={"phase": "submission"},
        )
        assert response.status_code == 200
        assert response.json()["phase"] == "submission"
        context = response.json()["submission_context"]
        # Every launch mode offers the same fixed candidate set.
        assert [item["id"] for item in context["fault_ontology"]] == fault_candidates()
        assert "node/pc1" in {item["id"] for item in context["resources"]}

        repeated = client.post(
            "/gateway/sessions/sess-1/phase",
            headers=_phase_headers(),
            json={"phase": "submission", "diagnosis_report": "replacement"},
        )
        assert repeated.status_code == 200
        assert repeated.json()["submission_context"] == context
        assert context["diagnosis_report"] == "already frozen"

    def test_health_endpoint(self) -> None:
        client = TestClient(create_gateway_app())
        response = client.get("/gateway/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_advance_phase_requires_host_phase_token(self, tmp_path: Path) -> None:
        """The agent knows its session id; that alone must not open submission."""
        register_session(
            "sess-1",
            scenario_name="simple_bgp",
            session_dir=str(tmp_path),
        )
        client = TestClient(create_gateway_app())
        response = client.post(
            "/gateway/sessions/sess-1/phase",
            headers={"NIKA-Session-Id": "sess-1"},
            json={"phase": "submission", "diagnosis_report": "link down on pc1"},
        )
        assert response.status_code == 403
        assert get_session("sess-1").phase == "diagnosis"
