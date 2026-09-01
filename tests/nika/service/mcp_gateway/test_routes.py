from __future__ import annotations

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

    def test_advance_phase_via_http(self) -> None:
        register_session("sess-1", scenario_name="simple_bgp")
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
