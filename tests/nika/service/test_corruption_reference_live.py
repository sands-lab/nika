"""Repeated reference probes use available client ports and current packets."""

import asyncio
from dataclasses import asdict
import json

import pytest

from nika.agents.mcp_config import engine_gateway
from nika.agents.oracle.agent import OracleAgent
from nika.benchmark_engine.protocol import GatewayRequest
from nika.runtime.factory import runtime_for_session
from nika.utils.session import Session
from nika.utils.session_id import resolve_session_tag
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import prepare_failure
from nika.workflows.session.close import close_session
from tests.support.prerequisites import docker_available

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not docker_available(), reason="Requires Docker")
def test_repeated_corruption_reference_probes_and_recovery(tmp_path):
    sid = start_net_env(
        "enterprise_branch",
        "s",
        telemetry=[],
        traffic={"pattern": "uniform", "load_mbps": 1.0},
        session_tag=resolve_session_tag(context="test"),
    )
    rounds = []
    runtime = None
    failure = None
    params = {"forwarding_device": "hq_edge", "intf_name": "eth4", "seed": 42}
    try:
        session = Session().load_running_session(session_id=sid)
        runtime = runtime_for_session(session)

        def observe(phase):
            agent = OracleAgent(
                sid,
                stream_output=False,
                planned={"device_forwarding_packet_corruption": params},
            )
            asyncio.run(agent.diagnose("Diagnose the running network."))
            assert len(agent.verdicts) == 1
            verdict = agent.verdicts[0]
            rounds.append(
                {
                    "phase": phase,
                    "confirmed": verdict.confirmed,
                    "error": verdict.error,
                    "steps": [asdict(step) for step in verdict.steps],
                }
            )
            assert verdict.error is None
            transfers = [
                step for step in verdict.steps if "short TCP transfers" in step.purpose
            ]
            assert len(transfers) == 2
            assert all("local_errors=0" in step.excerpt for step in transfers)
            if phase in {"healthy", "recovered"}:
                assert not verdict.confirmed
                assert all("failed=0 " in step.excerpt for step in transfers)
            else:
                assert verdict.confirmed, rounds[-1]
                assert all(step.passed for step in verdict.steps if step.evidence)

        with engine_gateway(sid, session.session_dir, GatewayRequest()):
            observe("healthy")
            failure = prepare_failure(
                session, ["device_forwarding_packet_corruption"], params
            )
            failure.inject()
            assert failure.verify()["verified"]
            observe("injected-first")
            observe("injected-repeat")
            assert failure.recover()["verified"]
            failure = None
            observe("recovered")
    finally:
        (tmp_path / "reference-rounds.json").write_text(
            json.dumps(rounds, indent=2) + "\n"
        )
        try:
            if failure is not None:
                failure.recover()
        finally:
            close_session(session_id=sid)
            if runtime is not None:
                assert not runtime.exists()
                assert not runtime.has_leftover_resources()
