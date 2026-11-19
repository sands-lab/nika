"""ICMP validation preserves its measurements without retaining capture fixtures."""

import asyncio
import json
from pathlib import Path

import pytest
from langchain_mcp_adapters.client import MultiServerMCPClient

from nika.agents.mcp_config import engine_gateway, load_session_mcp_config
from nika.agents.oracle.agent import tool_text
from nika.benchmark_engine.protocol import GatewayRequest
from nika.runtime.factory import runtime_for_session
from nika.utils.session import Session
from nika.utils.session_id import resolve_session_tag
from nika.validation.symptom.icmp_probes import _delivered
from nika.net_env.utils.iosxr.common import IMAGE as XR_IMAGE
from nika.net_env.utils.routeros.common import IMAGE as ROUTEROS_IMAGE
from nika.validation.presence import bind_injected_problem, clear_injected_problem
from nika.validation.symptom.snapshot import capture_task_baseline, evaluate_on_snapshot
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import inject_prepared_failure, prepare_failure
from nika.workflows.session.close import close_session
from tests.support.prerequisites import docker_available, docker_image_available

pytestmark = pytest.mark.integration
DEPLOYMENTS = json.loads(
    (Path(__file__).parents[2] / "fixtures/icmp_baseline_deployments.json").read_text()
)


@pytest.mark.parametrize("spec", DEPLOYMENTS, ids=lambda spec: spec["id"])
@pytest.mark.skipif(not docker_available(), reason="Requires Docker")
def test_icmp_baseline_and_live_symptoms_remove_owned_captures(tmp_path, spec):
    image = {"iosxr": XR_IMAGE, "routeros": ROUTEROS_IMAGE}.get(spec["device_profile"])
    if image is not None and not docker_image_available(image):
        pytest.skip("Requires selected native router image")
    fault = "icmp_frag_needed_filter_misconfiguration"
    deployment = {key: spec[key] for key in ("scenario", "topo_size", "device_profile")}
    sid = start_net_env(
        **deployment,
        telemetry=["netflow", "pingmesh", "prometheus", "syslog"],
        traffic={"pattern": "uniform", "load_mbps": 1.0},
        session_tag=resolve_session_tag(context="test"),
    )
    calls, checks = [], []
    runtime = active = None
    try:
        session = Session().load_running_session(session_id=sid)
        runtime = runtime_for_session(session)
        row = {**deployment, "problem": fault, "inject": spec["inject"]}
        with engine_gateway(sid, session.session_dir, GatewayRequest()):
            baseline = capture_task_baseline(sid, row)
            checks.append({"phase": "baseline", "result": baseline})
            assert baseline["ok"], baseline
            ends = next(iter(baseline["members"].values()))["evidence"]
            assert ends["code4"] >= 5 and ends["code3"] >= 5

            async def inspect(phase):
                tools = {
                    tool.name: tool
                    for tool in await MultiServerMCPClient(
                        connections=load_session_mcp_config(
                            sid, spec["scenario"], phase="diagnosis"
                        )
                    ).get_tools()
                }
                for command in (
                    "ps -eo args",
                    "find /tmp -maxdepth 1 \\( -name 'capture-*' "
                    "-o -name 'probe-icmp.pcap' \\) -print",
                ):
                    args = {
                        "host_name": ends["receiver"],
                        "command": command,
                        "timeout": 15,
                    }
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
                        assert "tcpdump -ni " not in output
                        assert "probe-icmp" not in output
                    else:
                        assert not output.strip(), output

            asyncio.run(inspect("baseline"))
            healthy_probe = prepare_failure(session, [fault], spec["inject"])
            bind_injected_problem(sid, healthy_probe.problem, healthy_probe.params)
            healthy = evaluate_on_snapshot(sid, sid)
            checks.append({"phase": "healthy", "result": healthy})
            assert not healthy["ok"], healthy
            assert healthy["payload"]["details"]["code4"] >= 5
            assert healthy["payload"]["details"]["code3"] >= 5
            clear_injected_problem(sid)
            asyncio.run(inspect("healthy"))
            active = prepare_failure(session, [fault], spec["inject"])
            inject_prepared_failure(
                session, [fault], active, param_overrides=spec["inject"]
            )
            for phase in ("injected", "repeated"):
                symptom = evaluate_on_snapshot(sid, sid)
                checks.append({"phase": phase, "result": symptom})
                assert symptom["ok"], symptom
                assert symptom["payload"]["details"]["code4"] == 0
                assert symptom["payload"]["details"]["code3"] >= 5
                asyncio.run(inspect(phase))
            assert active.recover()["verified"]
            active = None
            recovered = evaluate_on_snapshot(sid, sid)
            checks.append({"phase": "recovered", "result": recovered})
            assert not recovered["ok"], recovered
            assert recovered["payload"]["details"]["code4"] >= 5
            assert recovered["payload"]["details"]["code3"] >= 5
            asyncio.run(inspect("recovered"))

            async def read(node, command, phase):
                tools = {
                    tool.name: tool
                    for tool in await MultiServerMCPClient(
                        connections=load_session_mcp_config(
                            sid, spec["scenario"], phase="diagnosis"
                        )
                    ).get_tools()
                }
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
                return output

            operator_file = "/tmp/probe-icmp.pcap"
            runtime.write_file(ends["receiver"], operator_file, "operator-owned\n")
            try:
                with pytest.raises(ValueError, match="missing-sender"):
                    _delivered(runtime, {**ends, "sender": "missing-sender"})
                observed = asyncio.run(
                    read(
                        ends["receiver"],
                        "cat /tmp/probe-icmp.pcap",
                        "operator control after failure",
                    )
                )
                assert observed.strip() == "operator-owned"
                remaining = asyncio.run(
                    read(
                        ends["receiver"],
                        "find /tmp -maxdepth 1 -type f -name 'capture-*' -print",
                        "cleanup after sender failure",
                    )
                )
                assert not remaining.strip(), remaining
            finally:
                runtime.exec(ends["receiver"], "rm -f -- /tmp/probe-icmp.pcap")
    finally:
        (tmp_path / "icmp-baseline-calls.json").write_text(
            json.dumps(
                {
                    "deployment": spec,
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
                cleanup = {
                    "session_id": sid,
                    "runtime_exists": runtime.exists(),
                    "leftover_resources": runtime.has_leftover_resources(),
                }
                (tmp_path / "cleanup.json").write_text(json.dumps(cleanup, indent=2))
                assert not cleanup["runtime_exists"]
                assert not cleanup["leftover_resources"]
