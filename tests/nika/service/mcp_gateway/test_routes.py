from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from nika.mcp.gateway.app import create_gateway_app
from nika.mcp.gateway.session_registry import clear_sessions, register_session


class GatewayPhaseRouteTest:
    @pytest.fixture(autouse=True)
    def _setup(self) -> None:
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
            headers={"NIKA-Session-Id": "sess-1"},
            json={"phase": "submission", "diagnosis_report": "link down on pc1"},
        )
        assert response.status_code == 200
        assert response.json()["phase"] == "submission"

        messages = (session_dir / "messages.jsonl").read_text(encoding="utf-8")
        event = json.loads(messages.strip().splitlines()[-1])
        assert event["event"] == "diagnosis_frozen"
        assert event["report"] == "link down on pc1"

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
            headers={"NIKA-Session-Id": "sess-1"},
            json={"phase": "submission"},
        )
        assert response.status_code == 400
        assert "diagnosis_report" in response.json()["error"]

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
            headers={"NIKA-Session-Id": "sess-1"},
            json={"phase": "submission"},
        )
        assert response.status_code == 200
        assert response.json()["phase"] == "submission"

    def test_health_endpoint(self) -> None:
        client = TestClient(create_gateway_app())
        response = client.get("/gateway/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
