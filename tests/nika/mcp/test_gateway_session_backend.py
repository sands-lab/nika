"""Gateway server selection follows backend and device_profile on the session."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nika.mcp.gateway.lifecycle import mcp_gateway_for_session
from nika.mcp.gateway.session_registry import clear_sessions, get_session
from nika.run_config.schema import AgentAccessSettings

pytestmark = pytest.mark.unit


def _config() -> SimpleNamespace:
    return SimpleNamespace(agent=SimpleNamespace(access=AgentAccessSettings()))


@pytest.mark.parametrize(
    ("device_profile", "expected", "absent"),
    [
        ("sonic", "containerlab_sonic_mcp_server", "containerlab_srl_mcp_server"),
        (
            "nokia_srlinux",
            "containerlab_srl_mcp_server",
            "containerlab_sonic_mcp_server",
        ),
        (None, "containerlab_srl_mcp_server", "containerlab_sonic_mcp_server"),
    ],
)
def test_gateway_without_backend_uses_stored_session(
    device_profile: str | None, expected: str, absent: str
) -> None:
    row = {
        "session_id": "sess-isp",
        "scenario_name": "isp_dfn-bwin",
        "backend": "containerlab",
        "session_dir": "/tmp/sess-isp",
        "scenario_params": {
            "backend": "containerlab",
            **({"device_profile": device_profile} if device_profile else {}),
        },
    }
    manager = MagicMock()
    manager.base_url = "http://127.0.0.1:9"
    manager.backend = "containerlab"
    clear_sessions()
    try:
        with (
            patch(
                "nika.utils.session_store.SessionStore.get_session",
                return_value=row,
            ),
            patch(
                "nika.mcp.gateway.lifecycle.start_gateway",
                return_value=manager,
            ) as start,
            patch("nika.mcp.gateway.lifecycle._shutdown_manager"),
            patch(
                "nika.mcp.gateway.lifecycle.node_roles_for_session",
                return_value={},
            ),
            patch("nika.run_config.loader.get_run_config", return_value=_config()),
        ):
            with mcp_gateway_for_session("sess-isp", scenario_name="isp_dfn-bwin"):
                assert start.call_args.kwargs["backend"] == "containerlab"
                servers = get_session("sess-isp").diagnosis_servers
        assert expected in servers
        assert absent not in servers
    finally:
        clear_sessions()
