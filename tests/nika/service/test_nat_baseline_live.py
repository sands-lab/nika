"""NAT image baselines retain workload traffic and no probe fixtures."""

import asyncio
import json

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from nika.agents.mcp_config import engine_gateway, load_session_mcp_config
from nika.agents.oracle.agent import tool_text
from nika.benchmark_engine.protocol import GatewayRequest
from nika.net_env.utils.iosxr.common import IMAGE as XR_IMAGE
from nika.runtime.factory import runtime_for_session
from nika.utils.session import Session
from nika.utils.session_id import resolve_session_tag
from nika.validation.symptom.snapshot import capture_task_baseline, evaluate_on_snapshot
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import inject_prepared_failure, prepare_failure
from nika.workflows.session.close import close_session
from tests.support.prerequisites import docker_available, docker_image_available

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("profile", ["frr", "iosxr"])
@pytest.mark.skipif(not docker_available(), reason="Requires Docker")
def test_nat_image_baselines_and_live_symptoms_leave_no_probe_processes(
    tmp_path, profile
):
    if profile == "iosxr" and not docker_image_available(XR_IMAGE):
        pytest.skip("Requires the IOS XR image")
    sid = start_net_env(
        "enterprise_branch",
        "s",
        device_profile=profile,
        telemetry=[],
        traffic={"pattern": "uniform", "load_mbps": 1.0},
        session_tag=resolve_session_tag(context="test"),
    )
    calls, checks = [], []
    runtime = active = None
    try:
        session = Session().load_running_session(session_id=sid)
        runtime = runtime_for_session(session)

        async def inspect(phase):
            client = MultiServerMCPClient(
                connections=load_session_mcp_config(
                    sid, "enterprise_branch", phase="diagnosis"
                )
            )
            tools = {tool.name: tool for tool in await client.get_tools()}
            for node in ("internet_web", "br1_guest_pc"):
                for command in (
                    "ps -eo args",
                    "find /tmp -maxdepth 1 -name 'probe-nat-*' -print",
                    "ss -Hltn 'sport = :18080'",
                ):
                    args = {"host_name": node, "command": command, "timeout": 15}
                    output = tool_text(await tools["exec_shell"].ainvoke(args))
                    calls.append(
                        {
                            "phase": phase,
                            "tool": "exec_shell",
                            "args": args,
                            "output": output,
                        }
                    )
                    if command.startswith("ps "):
                        assert (
                            "webapp" in output
                            if node == "internet_web"
                            else "webclient" in output
                        )
                        assert "probe-nat" not in output
                    else:
                        assert not output.strip(), output

        with engine_gateway(sid, session.session_dir, GatewayRequest()):
            for fault, params in (
                (
                    "snat_port_pool_exhaustion",
                    {
                        "host_name": "br1_edge",
                        "source_prefix": "10.1.40.0/24",
                        "public_ip": "198.18.1.10",
                        "port_start": 40000,
                        "port_end": 40063,
                    },
                ),
                (
                    "nat_mapping_removed_without_drain",
                    {
                        "host_name": "br1_edge",
                        "source_prefix": "10.1.40.0/24",
                        "nat_ip_a": "198.18.1.10",
                        "nat_ip_b": "198.18.1.11",
                        "wan_interface": "eth2",
                    },
                ),
            ):
                row = {
                    "scenario": "enterprise_branch",
                    "topo_size": "s",
                    "device_profile": profile,
                    "problem": fault,
                    "inject": params,
                }
                baseline = capture_task_baseline(sid, row)
                checks.append(
                    {"fault": fault, "phase": "healthy baseline", "result": baseline}
                )
                assert baseline["ok"], baseline
                asyncio.run(inspect(fault + ": baseline"))
                active = prepare_failure(session, [fault], params)
                inject_prepared_failure(
                    session, [fault], active, param_overrides=params
                )
                symptom = evaluate_on_snapshot(sid, sid)
                checks.append({"fault": fault, "phase": "injected", "result": symptom})
                assert symptom["ok"], symptom
                asyncio.run(inspect(fault + ": injected"))
                assert active.recover()["verified"]
                active = None
                recovered = evaluate_on_snapshot(sid, sid)
                checks.append(
                    {"fault": fault, "phase": "recovered", "result": recovered}
                )
                assert not recovered["ok"], recovered
                asyncio.run(inspect(fault + ": recovered"))
    finally:
        (tmp_path / "nat-baseline-calls.json").write_text(
            json.dumps(
                {
                    "profile": profile,
                    "session_id": sid,
                    "calls": calls,
                    "checks": checks,
                },
                indent=2,
            )
            + "\n"
        )
        try:
            if active is not None:
                active.recover()
        finally:
            close_session(session_id=sid)
            if runtime is not None:
                assert not runtime.exists()
                assert not runtime.has_leftover_resources()
