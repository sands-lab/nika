"""P4 loss validation leaves no diagnostic receivers or logs in case images."""

import asyncio
import json

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from nika.agents.mcp_config import engine_gateway, load_session_mcp_config
from nika.agents.oracle.agent import tool_text
from nika.benchmark_engine.protocol import GatewayRequest
from nika.net_env.p4_dc_gateway.topology_model import build_gateway_fabric_model
from nika.runtime.factory import runtime_for_session
from nika.utils.session import Session
from nika.utils.session_id import resolve_session_tag
from nika.validation.presence import bind_injected_problem, clear_injected_problem
from nika.validation.symptom.snapshot import capture_task_baseline, evaluate_on_snapshot
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import inject_prepared_failure, prepare_failure
from nika.workflows.session.close import close_session
from tests.support.prerequisites import docker_available

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not docker_available(), reason="Requires Docker")
def test_p4_loss_baseline_and_live_symptoms_leave_no_probe_processes(tmp_path):
    sid = start_net_env(
        "p4_dc_gateway",
        "l",
        telemetry=["netflow", "pingmesh", "prometheus", "syslog"],
        traffic={"pattern": "uniform", "load_mbps": 1.0},
        session_tag=resolve_session_tag(context="test"),
    )
    calls, checks = [], []
    runtime = active = None
    try:
        session = Session().load_running_session(session_id=sid)
        runtime = runtime_for_session(session)
        model = build_gateway_fabric_model("l")
        port = model.port_to_peer("spine_1", "leaf_2")
        params = {
            "host_name": "spine_1",
            "intf_name": port.name,
            "bmv2_port": port.bmv2_port,
            "loss_basis_points": 200,
            "seed": 42,
        }
        row = {
            "scenario": "p4_dc_gateway",
            "topo_size": "l",
            "problem": "silent_egress_packet_loss",
            "inject": params,
        }
        services = [
            model.services_on_leaf(peer)[0].name for peer in ("leaf_1", "leaf_2")
        ]

        async def inspect(phase):
            tools = {
                tool.name: tool
                for tool in await MultiServerMCPClient(
                    connections=load_session_mcp_config(
                        sid, "p4_dc_gateway", phase="diagnosis"
                    )
                ).get_tools()
            }
            for node in services:
                for command in (
                    "ps -eo args",
                    "find /tmp -maxdepth 1 -name 'probe-udp-loss*' -print",
                    "ss -Huln '( sport = :19407 )'",
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
                        assert "probe-udp-loss" not in output
                        assert "sock.recvfrom(2048)" not in output
                    else:
                        assert not output.strip(), output

        with engine_gateway(sid, session.session_dir, GatewayRequest()):
            baseline = capture_task_baseline(sid, row)
            checks.append({"phase": "baseline", "result": baseline})
            assert baseline["ok"], baseline
            asyncio.run(inspect("baseline"))
            # Bind the verifier to the healthy lab without injecting the fault.
            healthy_probe = prepare_failure(session, [row["problem"]], params)
            bind_injected_problem(sid, healthy_probe.problem, healthy_probe.params)
            healthy = evaluate_on_snapshot(sid, sid)
            checks.append({"phase": "healthy", "result": healthy})
            assert not healthy["ok"], healthy
            assert healthy["payload"]["details"]["port_loss_percent"] == 0
            assert healthy["payload"]["details"]["other_loss_percent"] == 0
            clear_injected_problem(sid)
            asyncio.run(inspect("healthy"))
            active = prepare_failure(session, [row["problem"]], params)
            inject_prepared_failure(
                session, [row["problem"]], active, param_overrides=params
            )
            for phase in ("injected", "repeated"):
                symptom = evaluate_on_snapshot(sid, sid)
                checks.append({"phase": phase, "result": symptom})
                assert symptom["ok"], symptom
                asyncio.run(inspect(phase))
            assert active.recover()["verified"]
            active = None
            recovered = evaluate_on_snapshot(sid, sid)
            checks.append({"phase": "recovered", "result": recovered})
            assert not recovered["ok"], recovered
            asyncio.run(inspect("recovered"))
    finally:
        (tmp_path / "p4-loss-baseline-calls.json").write_text(
            json.dumps({"session_id": sid, "calls": calls, "checks": checks}, indent=2)
            + "\n"
        )
        try:
            if active is not None:
                active.recover()
        finally:
            close_session(session_id=sid)
            if runtime is not None:
                cleanup = {
                    "session_id": sid,
                    "runtime_exists": runtime.exists(),
                    "leftover_resources": runtime.has_leftover_resources(),
                }
                (tmp_path / "cleanup.json").write_text(json.dumps(cleanup, indent=2))
                assert not cleanup["runtime_exists"]
                assert not cleanup["leftover_resources"]
