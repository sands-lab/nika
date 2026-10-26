from __future__ import annotations

import pytest
import asyncio
import json
import time
from typing import Any, ClassVar
from unittest.mock import patch
from langchain_mcp_adapters.client import MultiServerMCPClient
from nika.agents.mcp_config import MCPServerConfig
from tests.support.integration_base import PerTestEnvTestCase
from tests.support.integration_pipeline import tool_text_list
from tests.support.prerequisites import docker_available

pytestmark = pytest.mark.integration


def _invoke_pingmesh(
    session_id: str,
    *,
    scenario_name: str,
    **tool_args: Any,
) -> dict:
    from nika.mcp.gateway.lifecycle import mcp_gateway_for_session

    # The assertions parse the full snapshot JSON; agent-facing output bounding
    # would elide the middle of large meshes.
    with (
        patch("nika.mcp.gateway.middleware.tool_output_max_chars", return_value=0),
        mcp_gateway_for_session(session_id, scenario_name=scenario_name),
    ):
        config = MCPServerConfig(session_id=session_id).load_http_config(
            ["pingmesh_mcp_server"]
        )

        async def _run() -> dict:
            client = MultiServerMCPClient(connections=config)
            tools = {tool.name: tool for tool in await client.get_tools()}
            raw = await tools["run_pingmesh_snapshot"].ainvoke(tool_args)
            texts = tool_text_list(raw)
            if not texts:
                raise AssertionError("PingMesh tool returned empty output")
            return json.loads(texts[0])

        return asyncio.run(_run())


def _wait_healthy_pingmesh(
    session_id: str,
    *,
    scenario_name: str,
    timeout_sec: float = 180.0,
    poll_sec: float = 10.0,
    **tool_args: Any,
) -> dict:
    """Poll until the mesh reports zero anomalies (post-deploy convergence)."""
    deadline = time.time() + timeout_sec
    last: dict | None = None
    while time.time() < deadline:
        last = _invoke_pingmesh(session_id, scenario_name=scenario_name, **tool_args)
        if last.get("summary", {}).get("anomaly_count") == 0:
            return last
        time.sleep(poll_sec)
    assert last is not None
    raise AssertionError(
        "pingmesh did not become healthy within "
        f"{timeout_sec}s: summary={last.get('summary')} "
        f"anomalies={last.get('anomalies', [])[:8]}"
    )


class KatharaPingMeshIntegrationTest(PerTestEnvTestCase):
    SCENARIO = "enterprise_branch"
    ENV_RUN_ARGS: ClassVar[list[str]] = ["-s", "m"]
    MIN_ENDPOINTS = 6
    INJECT_PARAMS: ClassVar[dict[str, str]] = {
        "host_name": "br1_corp_pc",
        "intf_name": "eth0",
    }
    FAULT_SOURCE = "br1_corp_pc"
    FAULT_PEER = "hq_corp_pc"

    @pytest.fixture(scope="class", autouse=True)
    def _setup_class(cls) -> None:
        if not docker_available():
            pytest.skip("Docker is not available")

    def test_pingmesh_healthy_then_faulty(self) -> None:
        # The scenario declares guest/IoT VRF isolation and the WAN RTT, so the
        # default mesh over every endpoint must read healthy without tuning.
        healthy = _wait_healthy_pingmesh(
            self.session_id, scenario_name=self.SCENARIO, include_healthy=True
        )
        endpoints = set(healthy["endpoints"])
        assert len(endpoints) >= self.MIN_ENDPOINTS, (
            f"expected at least {self.MIN_ENDPOINTS} endpoints, got {endpoints}"
        )
        assert healthy["summary"]["anomaly_count"] == 0
        assert healthy["summary"]["isolated_confirmed"] > 0
        assert len(healthy["results"]) > 2, "expected more than a 2-host mesh"

        self._inject_failure("link_down", self.INJECT_PARAMS)
        self._assert_failure_injected("link_down")
        faulty = _invoke_pingmesh(
            self.session_id,
            scenario_name=self.SCENARIO,
            sources=[self.FAULT_SOURCE],
            targets=[self.FAULT_PEER],
        )

        assert faulty["summary"]["anomaly_count"] > 0

        assert any((a["source"] == self.FAULT_SOURCE for a in faulty["anomalies"])), (
            f"expected anomalies from {self.FAULT_SOURCE}, got {faulty['anomalies']}"
        )
