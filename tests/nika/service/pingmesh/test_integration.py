from __future__ import annotations

import pytest
import asyncio
import json
import time
from typing import Any, ClassVar
from langchain_mcp_adapters.client import MultiServerMCPClient
from agent.utils.mcp_servers import MCPServerConfig
from tests.support.integration_base import PerTestEnvTestCase
from tests.support.integration_pipeline import tool_text_list
from tests.support.prerequisites import docker_available, min3clos_prerequisites


def _invoke_pingmesh(
    session_id: str,
    *,
    scenario_name: str,
    **tool_args: Any,
) -> dict:
    from nika.mcp.gateway.lifecycle import mcp_gateway_for_session

    with mcp_gateway_for_session(session_id, scenario_name=scenario_name):
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


def _cross_pairs(snapshot: dict) -> list[dict]:
    return [row for row in snapshot["results"] if row["source"] != row["target"]]


def _corp_endpoints(snapshot: dict) -> list[str]:
    """Overlay-reachable corp hosts (guest/iot VRFs are local-only)."""
    return sorted(name for name in snapshot.get("endpoints", {}) if "_corp_" in name)


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
        # Discover endpoints, then mesh only overlay corp hosts. Guest/IoT VRFs
        # are local-only and must not be expected to form a healthy full mesh.
        discovery = _invoke_pingmesh(self.session_id, scenario_name=self.SCENARIO)
        endpoints = set(discovery["endpoints"])
        assert len(endpoints) >= self.MIN_ENDPOINTS, (
            f"expected at least {self.MIN_ENDPOINTS} endpoints, got {endpoints}"
        )
        assert "br1_corp_pc" in endpoints
        assert "hq_corp_pc" in endpoints

        corp = _corp_endpoints(discovery)
        assert len(corp) >= 2, f"expected corp endpoints, got {corp}"
        healthy = _wait_healthy_pingmesh(
            self.session_id,
            scenario_name=self.SCENARIO,
            sources=corp,
            targets=corp,
            # Site-edge underlay delay routinely exceeds the 100ms default.
            high_latency_ms=500.0,
        )

        assert healthy["summary"]["anomaly_count"] == 0
        cross = _cross_pairs(healthy)

        assert len(cross) > 2, "expected more than a 2-host cross mesh"

        assert all((row["reachable"] for row in cross))
        self._inject_failure("link_down", self.INJECT_PARAMS)
        self._assert_failure_injected("link_down")
        faulty = _invoke_pingmesh(
            self.session_id,
            scenario_name=self.SCENARIO,
            sources=corp,
            targets=corp,
            high_latency_ms=500.0,
        )

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
        healthy = _wait_healthy_pingmesh(
            self.session_id, scenario_name=self.SCENARIO, timeout_sec=120.0
        )

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
