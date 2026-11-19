"""The SNAT reference applies measured concurrency and removes its diagnostics."""

import asyncio
import json

import pytest

from nika.agents.mcp_config import engine_gateway, load_session_mcp_config
from nika.agents.oracle.agent import OracleAgent, tool_text
from nika.benchmark_engine.protocol import GatewayRequest
from nika.net_env.utils.routeros.common import IMAGE as ROS_IMAGE
from nika.runtime.factory import runtime_for_session
from nika.schedule import schedule_for_task
from nika.utils.session import Session
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import inject_prepared_failure, prepare_failure
from nika.workflows.schedule import SessionScheduler
from nika.workflows.session.close import close_session
from langchain_mcp_adapters.client import MultiServerMCPClient
from tests.support.prerequisites import docker_available, docker_image_available

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("profile", ["frr", "routeros"])
@pytest.mark.skipif(not docker_available(), reason="Requires Docker")
def test_snat_reference_uses_concurrency_and_cleans_up(tmp_path, profile):
    if profile == "routeros" and not docker_image_available(ROS_IMAGE):
        pytest.skip("Requires the RouterOS image and KVM")
    sid = start_net_env(
        "enterprise_branch",
        "s",
        device_profile=profile,
        telemetry=["netflow", "pingmesh", "prometheus", "syslog"],
        traffic={"pattern": "uniform", "load_mbps": 1.0},
        result_dir=tmp_path / "sessions",
    )
    runtime = active = scheduler = None
    records = []
    params = {
        "host_name": "br1_edge",
        "source_prefix": "10.1.40.0/24",
        "public_ip": "198.18.1.10",
        "port_start": 40000,
        "port_end": 40063,
    }
    try:
        session = Session().load_running_session(session_id=sid)
        runtime = runtime_for_session(session)
        healthy = {"scenario": "enterprise_branch", "problem": "healthy", "inject": {}}
        scheduler = SessionScheduler(sid, schedule_for_task(healthy))
        scheduler.prepare()
        scheduler.start()
        scheduler.wait_agent_start()
        session = Session().load_running_session(session_id=sid)

        async def observe(phase):
            agent = OracleAgent(
                sid, stream_output=False, planned={"snat_port_pool_exhaustion": params}
            )
            await agent.diagnose(session.task_description)
            verdict = agent.verdicts[0]
            steps = [s.__dict__ for s in verdict.steps]
            records.append(
                {"phase": phase, "confirmed": verdict.confirmed, "steps": steps}
            )
            assert verdict.confirmed is (phase == "injected"), records[-1]
            assert steps[-1]["passed"] is True, steps[-1]
            pressure = next(
                s for s in steps if s["purpose"].startswith("Hold bounded HTTP")
            )
            assert pressure["passed"] is (phase == "injected"), pressure
            client = MultiServerMCPClient(
                connections=load_session_mcp_config(
                    sid, "enterprise_branch", phase="diagnosis"
                )
            )
            tools = {t.name: t for t in await client.get_tools()}
            result = await tools["exec_shell"].ainvoke(
                {
                    "host_name": "br1_guest_pc",
                    "command": "find /tmp -maxdepth 1 -type d -name 'nat-check-*' -print",
                    "timeout": 10,
                }
            )
            assert not tool_text(result).strip(), result

        with engine_gateway(sid, session.session_dir, GatewayRequest()):
            asyncio.run(observe("healthy"))
            active = prepare_failure(session, ["snat_port_pool_exhaustion"], params)
            inject_prepared_failure(
                session, ["snat_port_pool_exhaustion"], active, param_overrides=params
            )
            # The reference runs before any audit symptom burst can fill the pool.
            asyncio.run(observe("injected"))
            assert active.recover()["verified"]
            active = None
            asyncio.run(observe("recovered"))
    finally:
        (tmp_path / "snat-reference.json").write_text(
            json.dumps(
                {"profile": profile, "session": sid, "records": records}, indent=2
            )
        )
        try:
            if active is not None:
                active.recover()
            if scheduler is not None:
                scheduler.close()
        finally:
            close_session(session_id=sid)
            if runtime is not None:
                assert not runtime.exists()
                assert not runtime.has_leftover_resources()
