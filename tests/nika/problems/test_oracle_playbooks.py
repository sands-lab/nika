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


@pytest.mark.contract
@pytest.mark.parametrize("profile", ["nokia_srlinux", "sonic"])
def test_min3clos_oracle_covers_slow_flaps_and_declared_tcp_service(profile) -> None:
    from nika.net_env.min3clos.lab import ContainerlabMin3Clos
    from nika.problems.registry import get_problem_class
    from nika.workflows.benchmark.inject_resolve import resolve_inject_params

    options = {"backend": "containerlab", "device_profile": profile}
    for failure in ("link_flap", "device_forwarding_packet_corruption"):
        cls = get_problem_class(failure)
        problem = cls(
            "min3clos",
            topology_file=ContainerlabMin3Clos(**options).topology_template(),
            **options,
        )
        params = problem.parse_params(
            resolve_inject_params(failure, "min3clos", "", seed=1, isp_options=options)
        )
        playbook = problem.oracle_playbook(params)
        if failure == "link_flap":
            period = params.down_time + params.up_time
            evidence = [step for step in playbook.steps if step.evidence]
            assert len(evidence) == 2
            assert evidence[0].args["timeout"] > 2 * period
            assert evidence[1].args["timeout"] > 3 * period
            assert f"+{3 * period}" in evidence[1].args["command"]
            assert "-W 0.1" in evidence[1].args["command"]
        else:
            assert problem.net_env.servers["web"] == ["client2"]
            address = playbook.steps[0]
            assert address.args["host_name"] == "client2"
            assert "dev eth1 scope global" in address.args["command"]
            checksum = next(step for step in playbook.steps if step.evidence)
            assert (
                "before=131"
                in checksum.args({"corrupt_before": "TcpInCsumErrors 131 0.0"})[
                    "command"
                ]
            )
            assert checksum.expect("TcpInCsumErrorsDelta 5")
            assert not checksum.expect("TcpInCsumErrorsDelta 0")
            with pytest.raises(ValueError, match="baseline"):
                checksum.args({})
