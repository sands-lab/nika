"""Verify image deployment parameters against a native lab and its real gateway."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from nika.agents.mcp_config import engine_gateway, load_session_mcp_config
from nika.agents.oracle.agent import tool_text
from nika.benchmark_engine.protocol import GatewayRequest
from nika.firecracker.image import variant_for_row
from nika.remote.protocol import EnvStartRequest
from nika.utils.session_store import SessionStore
from nika.workflows.env.start import start_net_env
from nika.workflows.session.close import close_session
from tests.support.prerequisites import docker_available

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not docker_available(), reason="Docker required"),
]


def test_image_deployment_preserves_declared_telemetry():
    request = EnvStartRequest(
        **variant_for_row(
            {
                "scenario": "dc_clos",
                "topo_size": "s",
                "telemetry": ["netflow", "pingmesh", "prometheus", "syslog"],
                "traffic": {"pattern": "uniform", "load_mbps": 1.0},
            }
        )
    )
    sid = start_net_env(
        request.scenario,
        request.topo_size,
        session_tag="test-aud-telemetry",
        telemetry=request.telemetry,
        traffic=request.traffic,
    )
    try:
        directory = SessionStore().get_session(sid)["session_dir"]

        async def observe():
            client = MultiServerMCPClient(
                connections=load_session_mcp_config(sid, "dc_clos", phase="diagnosis")
            )
            tools = {t.name: t for t in await client.get_tools()}
            expected = {"prometheus_query", "syslog_query", "flow_coverage"}
            assert expected <= tools.keys(), sorted(tools)
            outputs = {}
            for name, args in [
                ("prometheus_query", {"query": "count(up)", "limit": 50}),
                ("prometheus_query", {"query": "count(probe_success)", "limit": 50}),
                ("flow_coverage", {}),
                ("syslog_query", {"start_time": "-300", "limit": 5}),
            ]:
                value = tool_text(await tools[name].ainvoke(args))
                assert not any(
                    marker in value
                    for marker in ("Traceback", "tool_execution_error", "[TIMEOUT]")
                ), value
                outputs[f"{name}:{args}"] = value
                if name == "prometheus_query":
                    assert json.loads(value)["value"] > 0, (args, value)
            Path(directory, "image-telemetry-evidence.json").write_text(
                json.dumps({"tools": sorted(tools), "outputs": outputs}, indent=2)
            )

        with engine_gateway(sid, directory, GatewayRequest()):
            asyncio.run(observe())
    finally:
        close_session(session_id=sid, undeploy=True, status="finished")
