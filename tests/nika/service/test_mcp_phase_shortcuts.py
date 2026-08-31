from __future__ import annotations

from nika.mcp.gateway.policy import is_server_allowed
from nika.mcp.gateway.session_registry import (
    advance_phase,
    clear_sessions,
    register_session,
)
import pytest


def test_failure_catalog_is_unavailable_during_diagnosis() -> None:
    clear_sessions()


def test_submission_phase_cannot_be_reversed() -> None:
    clear_sessions()
    register_session("phase-lock", scenario_name="simple_bgp")
    advance_phase("phase-lock", "submission")
    with pytest.raises(ValueError, match="cannot move back"):
        advance_phase("phase-lock", "diagnosis")
    clear_sessions()
    register_session("shortcut-test", scenario_name="dc_clos")
    assert is_server_allowed("shortcut-test", "kathara_base_mcp_server")
    assert not is_server_allowed("shortcut-test", "task_mcp_server")
    advance_phase("shortcut-test", "submission")
    assert is_server_allowed("shortcut-test", "task_mcp_server")
    assert not is_server_allowed("shortcut-test", "kathara_base_mcp_server")
    clear_sessions()
