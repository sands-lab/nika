"""Unit tests for SDN Clos MCP API and diagnosis server selection."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from nika.service.kathara.sdn_api import KatharaSdnAPI
from nika.service.mcp_server.registry import select_diagnosis_servers


def test_sdn_l3_clos_selects_sdn_mcp_server() -> None:
    servers = select_diagnosis_servers("sdn_l3_clos", backend="kathara")
    assert "kathara_sdn_mcp_server" in servers
    assert "kathara_bmv2_mcp_server" not in servers


def test_get_fabric_state_aggregates_sections() -> None:
    api = KatharaSdnAPI.__new__(KatharaSdnAPI)
    api.lab = MagicMock()
    api.lab.machines = {
        "leaf_1": MagicMock(),
        "spine_1": MagicMock(),
        "client_1_1": MagicMock(),
        "onos": MagicMock(),
        "fabric_mgr": MagicMock(),
    }
    for name, machine in api.lab.machines.items():
        machine.get_image.return_value = (
            "kathara/sdn" if name.startswith(("leaf_", "spine_")) else "nika/base"
        )

    def _exec(host: str, command: str, timeout: float = 15) -> str:
        if "/onos/v1/flows" in command and "/groups" not in command:
            return '{"flows":[{"deviceId":"of:0000000000001001","priority":40000}]}'
        if "/onos/v1/groups" in command:
            return '{"groups":[{"deviceId":"of:0000000000001001","id":4098}]}'
        if "/onos/v1/devices" in command or "/onos/v1/links" in command:
            return '{"devices":[],"links":[],"hosts":[]}'
        if "/onos/v1/hosts" in command:
            return '{"hosts":[]}'
        if "/onos/v1/applications" in command:
            return '{"applications":[]}'
        return "ok"

    with patch.object(KatharaSdnAPI, "exec_cmd", side_effect=_exec):
        state = api.sdn_get_fabric_state("s", switch_name="leaf_1")

    assert set(state) >= {
        "onos_topology",
        "controller_apps",
        "controller_programmed_intent",
        "controller_live_state",
        "switch_observed_state",
    }
    intent = state["controller_live_state"]
    assert intent["source"] == "onos_live"
    assert "leaf_1" in state["switch_observed_state"]
    assert all(f.get("deviceId") == "of:0000000000001001" for f in intent["flows"])
