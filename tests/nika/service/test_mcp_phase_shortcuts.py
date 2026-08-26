from __future__ import annotations

from nika.service.mcp_gateway.policy import is_server_allowed
from nika.service.mcp_gateway.session_registry import (
    advance_phase,
    clear_sessions,
    register_session,
)


def test_failure_catalog_is_unavailable_during_diagnosis() -> None:
    clear_sessions()
    register_session("shortcut-test", scenario_name="dc_clos")
    assert is_server_allowed("shortcut-test", "kathara_base_mcp_server")
    assert not is_server_allowed("shortcut-test", "task_mcp_server")
    advance_phase("shortcut-test", "submission")
    assert is_server_allowed("shortcut-test", "task_mcp_server")
    assert not is_server_allowed("shortcut-test", "kathara_base_mcp_server")
    clear_sessions()
