"""Unit tests for SDN Clos MCP API and diagnosis server selection."""

from __future__ import annotations

import pytest

from unittest.mock import MagicMock, patch

from nika.service.kathara.sdn_api import KatharaSdnAPI
from nika.mcp.registry import select_diagnosis_servers

pytestmark = pytest.mark.unit


def test_sdn_l3_clos_selects_sdn_mcp_server() -> None:
    servers = select_diagnosis_servers("sdn_l3_clos", backend="kathara")
    assert "kathara_sdn_mcp_server" in servers
    assert "kathara_bmv2_mcp_server" not in servers


def test_sdn_onos_rest_and_ovs_exec() -> None:
    api = KatharaSdnAPI.__new__(KatharaSdnAPI)
    api.lab = MagicMock()

    def _exec(host: str, command: str, timeout: float = 15) -> str:
        if "/onos/v1/devices" in command:
            return '{"devices":[{"id":"of:1"}]}\n200'
        if "ovs-ofctl" in command:
            return "OFPST_FLOW reply"
        return "ok"

    with patch.object(KatharaSdnAPI, "exec_cmd", side_effect=_exec):
        rest = api.sdn_onos_rest("/onos/v1/devices")
        ovs = api.sdn_ovs_exec("leaf_1", "ovs-ofctl -O OpenFlow13 dump-flows leaf_1")

    assert rest["path"] == "/onos/v1/devices"
    assert rest["body"]["devices"][0]["id"] == "of:1"
    assert ovs["switch"] == "leaf_1"
    assert "OFPST_FLOW" in ovs["output"]


def test_sdn_onos_rest_pages_filtered_flows() -> None:
    import json

    from nika.mcp.tool_output import truncate_tool_text

    api = KatharaSdnAPI.__new__(KatharaSdnAPI)
    flows = [
        {"deviceId": "of:1", "id": str(i), "selector": {"criteria": ["x" * 900]}}
        for i in range(12)
    ] + [{"deviceId": "of:2", "id": "other"}]
    with patch.object(
        KatharaSdnAPI, "exec_cmd", return_value=json.dumps({"flows": flows}) + "\n200"
    ):
        offset = 0
        observed = []
        while offset is not None:
            page = api.sdn_onos_rest("/onos/v1/flows", "of:1", offset)
            assert page["summary"]["flows"]["count"] == 12
            assert "characters elided" not in truncate_tool_text(
                json.dumps(page, indent=2), max_chars=10000
            )
            observed.extend(item["id"] for item in page["body"]["flows"])
            offset = page["pagination"]["flows"]["next_offset"]
        assert observed == [str(i) for i in range(12)]
        with pytest.raises(ValueError, match="non-negative"):
            api.sdn_onos_rest("/onos/v1/flows", offset=-1)
