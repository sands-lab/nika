"""Flow blackhole evidence separates handshake loss from payload corruption."""

import json
import shlex

import pytest

from nika.agents.oracle.plan import fault_plan

pytestmark = pytest.mark.unit


@pytest.fixture
def plan():
    return fault_plan(
        "device_flow_blackhole",
        "enterprise_branch",
        {"backend": "kathara", "topo_size": "s"},
        {"forwarding_device": "hq_edge", "intf_name": "eth0", "seed": "42"},
    ).playbook


def test_workload_owned_service_is_probed_across_the_affected_egress(plan):
    assert plan.steps[0].args["host_name"] == "hq_srv"
    probe = plan.steps[1].args(
        {"flow_server": "2: eth0 inet 10.0.20.2/24 scope global eth0"}
    )
    assert probe["host_name"] == "hq_corp_pc"
    assert "sock.connect((address, 8080))" in probe["command"]


def test_evidence_requires_stable_handshake_loss_with_successful_control_flows(plan):
    check = plan.steps[1].expect
    results = {str(port): ["connected"] * 2 for port in range(40000, 40048)}
    prefix = "3 packets transmitted, 3 received, 0% packet loss\n"
    assert not check(prefix + json.dumps(results))
    results["40000"] = ["timeout"] * 2
    assert check(prefix + json.dumps(results))
    assert not check(
        "3 packets transmitted, 0 received, 100% packet loss\n" + json.dumps(results)
    )
    results["40000"] = ["connected", "timeout"]
    assert not check(prefix + json.dumps(results))
    results["40000"] = ["ConnectionRefusedError"] * 2
    assert not check(prefix + json.dumps(results))


def test_oracle_diagnoses_without_reading_injection_state(plan):
    commands = [
        step.args.get("command", "")
        for step in plan.steps
        if isinstance(step.args, dict)
    ]
    assert all("tc " not in command for command in commands)
    assert "BPF" not in plan.finding


def test_device_evidence_requires_two_independent_service_paths(plan):
    evidence = [step for step in plan.steps if step.evidence]
    assert len(evidence) == 2
    assert [step.args["host_name"] for step in plan.steps if step.save_as] == [
        "hq_srv",
        "internet_web",
    ]
    for step, key in zip(evidence, ("flow_server", "flow_server_1")):
        command = step.args({key: "2: eth0 inet 10.0.20.2/24 scope global eth0"})[
            "command"
        ]
        script = shlex.split(command.partition("; ")[2])[-1]
        compile(script, "<TCP probe>", "exec")
