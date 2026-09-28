"""MCP client config follows the backend recorded on the session."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.utils.mcp_client import load_session_mcp_config


@pytest.fixture
def gateway_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NIKA_MCP_GATEWAY_URL", "http://127.0.0.1:9")


def test_containerlab_session_selects_srl_server(gateway_url: None) -> None:
    row = {
        "session_id": "sess-clab",
        "scenario_name": "isp_pdh",
        "backend": "containerlab",
        "scenario_params": {},
    }
    with patch(
        "nika.utils.session_store.SessionStore.get_session",
        return_value=row,
    ):
        config = load_session_mcp_config("sess-clab", "isp_pdh")

    assert "containerlab_srl_mcp_server" in config
    assert "kathara_frr_mcp_server" not in config


def test_explicit_backend_overrides_session(gateway_url: None) -> None:
    row = {
        "session_id": "sess-clab",
        "scenario_name": "isp_pdh",
        "backend": "containerlab",
        "scenario_params": {},
    }
    with patch(
        "nika.utils.session_store.SessionStore.get_session",
        return_value=row,
    ):
        config = load_session_mcp_config(
            "sess-clab",
            "isp_pdh",
            backend="kathara",
        )

    # Kathara routing uses base exec_shell; FRR MCP is RPKI-only.
    assert "kathara_base_mcp_server" in config
    assert "containerlab_srl_mcp_server" not in config
    assert "kathara_frr_mcp_server" not in config


def test_missing_session_keeps_scenario_default(gateway_url: None) -> None:
    config = load_session_mcp_config("missing-session-id", "isp_pdh")

    assert "kathara_base_mcp_server" in config
    assert "containerlab_srl_mcp_server" not in config
    assert "kathara_frr_mcp_server" not in config


def test_rpki_scenario_selects_frr_server(gateway_url: None) -> None:
    config = load_session_mcp_config("missing-session-id", "isp_abilene_ebgp_rpki")

    assert "kathara_frr_mcp_server" in config
