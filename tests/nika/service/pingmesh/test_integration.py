from __future__ import annotations

import pytest
import asyncio
import json
from typing import ClassVar
from langchain_mcp_adapters.client import MultiServerMCPClient
from agent.utils.mcp_servers import MCPServerConfig
from tests.support.integration_base import PerTestEnvTestCase
from tests.support.integration_pipeline import tool_text_list
from tests.support.prerequisites import docker_available, min3clos_prerequisites


def _invoke_pingmesh(session_id: str, *, scenario_name: str) -> dict:
    from nika.service.mcp_gateway.lifecycle import mcp_gateway_for_session

    with mcp_gateway_for_session(session_id, scenario_name=scenario_name):
        config = MCPServerConfig(session_id=session_id).load_http_config(
            ["pingmesh_mcp_server"]
        )

        async def _run() -> dict:
            client = MultiServerMCPClient(connections=config)
            tools = {tool.name: tool for tool in await client.get_tools()}
            raw = await tools["run_pingmesh_snapshot"].ainvoke({})
            texts = tool_text_list(raw)
            if not texts:
                raise AssertionError("PingMesh tool returned empty output")
            return json.loads(texts[0])

        return asyncio.run(_run())


def _cross_pairs(snapshot: dict) -> list[dict]:
    return [row for row in snapshot["results"] if row["source"] != row["target"]]


class KatharaPingMeshIntegrationTest(PerTestEnvTestCase):
    SCENARIO = "enterprise_branch"
    ENV_RUN_ARGS: ClassVar[list[str]] = ["-s", "m"]
    MIN_ENDPOINTS = 6
    INJECT_PARAMS: ClassVar[dict[str, str]] = {
        "host_name": "br1_corp_pc",
        "intf_name": "eth0",
    }
    FAULT_SOURCE = "br1_corp_pc"

    @pytest.fixture(scope="class", autouse=True)
    def _setup_class(cls) -> None:
        if not docker_available():
            pytest.skip("Docker is not available")

    def test_pingmesh_healthy_then_faulty(self) -> None:
        healthy = _invoke_pingmesh(self.session_id, scenario_name=self.SCENARIO)
        endpoints = set(healthy["endpoints"])

        assert len(endpoints) >= self.MIN_ENDPOINTS, (
            f"expected at least {self.MIN_ENDPOINTS} endpoints, got {endpoints}"
        )

        assert "br1_corp_pc" in endpoints
        assert "hq_corp_pc" in endpoints

        assert healthy["summary"]["anomaly_count"] == 0
        cross = _cross_pairs(healthy)

        assert len(cross) > 2, "expected more than a 2-host cross mesh"

        assert all((row["reachable"] for row in cross))
        self._inject_failure("link_down", self.INJECT_PARAMS)
        self._assert_failure_injected("link_down")
        faulty = _invoke_pingmesh(self.session_id, scenario_name=self.SCENARIO)

        assert faulty["summary"]["anomaly_count"] > 0

        assert any((a["source"] == self.FAULT_SOURCE for a in faulty["anomalies"])), (
            f"expected anomalies from {self.FAULT_SOURCE}, got {faulty['anomalies']}"
        )


@pytest.mark.skipif(
    not min3clos_prerequisites(), reason="containerlab, gnmic, or Docker not available"
)
class ContainerlabPingMeshIntegrationTest(PerTestEnvTestCase):
    SCENARIO = "min3clos"
    ENDPOINTS: ClassVar[frozenset[str]] = frozenset({"client1", "client2"})
    INJECT_PARAMS: ClassVar[dict[str, str]] = {
        "host_name": "leaf1",
        "intf_name": "e1-1",
    }

    def test_pingmesh_healthy_then_faulty(self) -> None:
        healthy = _invoke_pingmesh(self.session_id, scenario_name=self.SCENARIO)

        assert set(healthy["endpoints"]) == self.ENDPOINTS

        assert healthy["summary"]["anomaly_count"] == 0
        cross = _cross_pairs(healthy)

        assert cross

        assert all((row["reachable"] for row in cross))
        self._inject_failure("link_down", self.INJECT_PARAMS)
        self._assert_failure_injected("link_down")
        faulty = _invoke_pingmesh(self.session_id, scenario_name=self.SCENARIO)

        assert faulty["summary"]["anomaly_count"] > 0
        anomaly_pairs = {(a["source"], a["target"]) for a in faulty["anomalies"]}

        assert ("client1", "client2") in anomaly_pairs or (
            "client2",
            "client1",
        ) in anomaly_pairs, f"expected client pair anomaly, got {anomaly_pairs}"
