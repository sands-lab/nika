"""P4 fault state, tool-visible Oracle evidence, and recovery on real switches."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from nika.net_env.p4_dc_fabric.fabric_manager.apply import load_intent
from nika.agents.oracle.agent import OracleAgent
from nika.agents.mcp_config import engine_gateway
from nika.benchmark_engine.protocol import GatewayRequest
from nika.utils.session import Session
from nika.utils.session_store import SessionStore
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import inject_failure, prepare_failure
from nika.workflows.session.close import close_session
from nika.validation.symptom.p4_runtime_probes import (
    table_exhaustion,
    table_exhaustion_baseline,
)
from tests.support.prerequisites import docker_available

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not docker_available(), reason="Docker required"),
]


def exercise_fault(session_id: str, name: str, overrides: dict) -> dict:
    session = Session()
    session.load_running_session(session_id=session_id)
    prepared = prepare_failure(session, [name], overrides)
    problem, params = prepared.problem, prepared.params
    baseline, symptom = table_exhaustion_baseline, table_exhaustion
    artifacts = Path(session.session_dir) / "p4-fault-validation"
    artifacts.mkdir(exist_ok=True)
    stem = f"{name}-{params.host_name}"
    results = {"failure": name, "params": params.model_dump()}
    intent = load_intent(problem.runtime)
    healthy, results["baseline"] = baseline(problem, params)
    assert healthy, results
    assert not problem.verify_fault(params)["verified"]
    injected = False
    try:
        injected = True
        inject_failure([name], session_id=session_id, param_overrides=overrides)
        # Recovery uses the same persisted ownership metadata as a resumed run.
        record = SessionStore().list_failure_injections(session_id=session_id)[0]
        for attr in problem.RECORDED_ATTRS:
            setattr(problem, attr, record["injection_params"][attr])
        results["artifact"] = problem.verify_fault(params)
        assert results["artifact"]["verified"], results
        assert load_intent(problem.runtime) == intent
        broken, results["symptom"] = symptom(problem, params)
        assert broken, results
        oracle = OracleAgent(session_id, stream_output=False)
        with engine_gateway(session_id, session.session_dir, GatewayRequest()):
            asyncio.run(
                oracle.diagnose(
                    "Diagnose the network incident using the available tools."
                )
            )
        results["oracle"] = json.loads(
            (Path(session.session_dir) / "oracle.json").read_text()
        )
        assert len(oracle.verdicts) == 1 and oracle.verdicts[0].confirmed, results[
            "oracle"
        ]
        assert oracle.verdicts[0].root_causes == [
            {"resource_id": f"node/{params.host_name}", "fault_type": name}
        ]
    finally:
        if injected:
            results["recovery"] = prepared.recover()
            assert prepared.recover()["verified"]
            assert load_intent(problem.runtime) == intent
        (artifacts / f"{stem}.json").write_text(json.dumps(results, indent=2))
    healthy, results["recovered_baseline"] = baseline(problem, params)
    assert healthy, results
    assert not problem.verify_fault(params)["verified"]
    # Keep the old injection record, then ask the actual Oracle to diagnose the
    # recovered network through MCP. It must abstain on this negative control.
    oracle = OracleAgent(session_id, stream_output=False)
    with engine_gateway(session_id, session.session_dir, GatewayRequest()):
        asyncio.run(oracle.diagnose("Diagnose the network using the available tools."))
    results["recovered_oracle"] = json.loads(
        (Path(session.session_dir) / "oracle.json").read_text()
    )
    assert len(oracle.verdicts) == 1 and not oracle.verdicts[0].confirmed, results[
        "recovered_oracle"
    ]
    (artifacts / f"{stem}.json").write_text(json.dumps(results, indent=2))
    return results


@pytest.mark.parametrize("scenario", ["p4_dc_gateway", "p4_dc_fabric"])
def test_p4_fault_state_and_oracle_recover(scenario, tmp_path):
    sid = start_net_env(
        scenario, "s", instance_tag="p4-fault-validation", result_dir=str(tmp_path)
    )
    try:
        exercise_fault(sid, "p4_table_resource_exhaustion", {"host_name": "leaf_1"})
    finally:
        close_session(session_id=sid)
    with pytest.raises(FileNotFoundError):
        SessionStore().get_session(sid)
