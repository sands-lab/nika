"""Contracts for the oracle solver's reference playbooks and submission rule."""

from __future__ import annotations

import json

import pytest

from nika.agents.oracle.agent import FaultVerdict, OracleAgent, StepResult
from nika.problems.base import ProblemBase
from nika.problems.registry import list_avail_problem_instances
from nika.problems.support.oracle import healthy_playbook

CAUSE = {"resource_id": "node/r1", "fault_type": "frr_service_down"}
CONTEXT = {"resources": [{"id": "node/r1", "kind": "node"}]}


def _step(passed: bool | None, *, evidence: bool = True) -> StepResult:
    return StepResult(1, "", "exec_shell", {}, evidence, None, passed, 1, "")


def _verdict(*steps: StepResult, anomaly: bool = True) -> FaultVerdict:
    causes = [CAUSE] if anomaly else []
    return FaultVerdict("x", "", anomaly, causes, steps=list(steps))


def _submission(*verdicts: FaultVerdict) -> dict:
    agent = OracleAgent.__new__(OracleAgent)
    agent.verdicts = list(verdicts)
    return agent._submission(CONTEXT)


@pytest.mark.contract
def test_every_failure_defines_an_oracle_playbook() -> None:
    missing = sorted(
        name
        for name, cls in list_avail_problem_instances().items()
        if cls.oracle_playbook is ProblemBase.oracle_playbook
    )
    assert not missing, f"failures without oracle_playbook(): {missing}"


@pytest.mark.unit
def test_confirmed_fault_submits_its_root_cause() -> None:
    verdict = _verdict(_step(None, evidence=False), _step(True))
    assert _submission(verdict) == {"is_anomaly": True, "root_causes": [CAUSE]}


@pytest.mark.unit
@pytest.mark.parametrize("steps", [(_step(False),), (_step(None, evidence=False),)])
def test_unconfirmed_fault_abstains(steps) -> None:
    assert _submission(_verdict(*steps)) is None


@pytest.mark.unit
def test_healthy_control_reports_healthy_only_when_confirmed() -> None:
    assert _submission(_verdict(_step(True), anomaly=False)) == {
        "is_anomaly": False,
        "root_causes": [],
    }
    assert _submission(_verdict(_step(False), anomaly=False)) is None


@pytest.mark.unit
def test_healthy_check_reads_pingmesh_summary() -> None:
    check = healthy_playbook().steps[0].expect

    def snapshot(anomalies: int, pairs: int = 6) -> str:
        return json.dumps(
            {"summary": {"total_pairs": pairs, "anomaly_count": anomalies}}
        )

    assert check(snapshot(0))
    assert not check(snapshot(2))
    assert not check(snapshot(0, pairs=0))
    assert not check("tool_error: timeout")
