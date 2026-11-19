"""Contracts for the oracle solver's reference playbooks and submission rule."""

from __future__ import annotations

import json

import pytest

from nika.agents.oracle.agent import FaultVerdict, OracleAgent, StepResult
from nika.problems.base import ProblemBase
from nika.problems.registry import list_avail_problem_instances
from nika.problems.support.oracle import healthy_playbook

CAUSE = {"resource_id": "node/r1", "fault_type": "routing_service_down"}
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


@pytest.mark.contract
def test_planned_repeated_faults_keep_both_placements():
    from types import SimpleNamespace
    from nika.problems.combinations import get_combination, task_placement
    from nika.workflows.benchmark.load_config import normalize_benchmark_row

    row = normalize_benchmark_row(
        get_combination("redundant_links_both_lost").rows()[0]
    )
    agent = OracleAgent.__new__(OracleAgent)
    agent.session = SimpleNamespace(
        scenario_params={"topo_size": "s", "backend": "kathara"}
    )
    agent.scenario_name = row["scenario"]
    agent.planned = task_placement(row)
    verdicts = agent._faults()
    assert len(verdicts) == 2
    assert all(v.fault_type == "link_down" and v.error is None for v in verdicts)
    assert verdicts[0].root_causes != verdicts[1].root_causes


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
def test_healthy_check_reads_recorded_probe_query() -> None:
    check = healthy_playbook().steps[0].expect
    assert check(json.dumps([{"value": 0}]))
    assert check(json.dumps({"value": 0}))
    assert not check(json.dumps([{"value": 2}]))
    assert not check("[]")
    assert not check("tool_error: timeout")


@pytest.mark.contract
@pytest.mark.parametrize("profile", ["nokia_srlinux", "sonic"])
def test_dc_clos_oracle_covers_flap_cycles_and_declared_tcp_service(
    profile, tmp_path
) -> None:
    from nika.problems.registry import get_problem_class
    from nika.workflows.benchmark.inject_resolve import resolve_inject_params

    import yaml
    from nika.net_env.net_env_pool import get_net_env_instance

    options = {"backend": "containerlab", "device_profile": profile}
    env = get_net_env_instance("dc_clos", topo_size="s", **options)
    topology = tmp_path / "clos.clab.yml"
    topology.write_text(yaml.safe_dump(env._document()))
    for failure in ("link_flap", "device_forwarding_packet_corruption"):
        cls = get_problem_class(failure)
        problem = cls(
            "dc_clos",
            topo_size="s",
            topology_file=topology,
            **options,
        )
        params = problem.parse_params(
            (
                {
                    "forwarding_device": "leaf_2",
                    "intf_name": "eth1" if profile == "sonic" else "e1-1",
                }
                if failure == "device_forwarding_packet_corruption"
                else resolve_inject_params(
                    failure, "dc_clos", "s", seed=1, isp_options=options
                )
            )
        )
        playbook = problem.oracle_playbook(params)
        if failure == "link_flap":
            period = params.down_time + params.up_time
            evidence = [step for step in playbook.steps if step.evidence]
            assert len(evidence) == 2
            assert evidence[0].attempts * evidence[0].interval_sec >= 2 * period
            assert "sleep" not in str(evidence[0].args)
            assert evidence[1].args["timeout"] > 3 * period
            assert f"+{max(12, 3 * period)}" in evidence[1].args["command"]
            assert "-W 0.1" in evidence[1].args["command"]
        else:
            assert "web_2" in problem.net_env.servers["web"]
            address = playbook.steps[0]
            assert address.args["host_name"] == "web_2"
            assert "dev eth0 scope global" in address.args["command"]
            checksum = next(
                step
                for step in playbook.steps
                if step.evidence
                and step.tool == "exec_shell"
                and step.expect is not None
                and step.expect.observations is not None
            )
            assert (
                checksum.args({"corrupt_before": "TcpInCsumErrors 131 0.0"})["command"]
                == "nstat -asz TcpInCsumErrors"
            )
            baseline = {"corrupt_before": "TcpInCsumErrors 131 0.0"}
            assert checksum.expect("TcpInCsumErrors 136 0.0", baseline)
            assert not checksum.expect("TcpInCsumErrors 131 0.0", baseline)
            assert not checksum.expect("TcpInCsumErrors 136 0.0", {})


@pytest.mark.unit
def test_oracle_uses_latest_successful_injection_target(monkeypatch):
    from types import SimpleNamespace
    from nika.utils.session_store import SessionStore
    import nika.agents.oracle.agent as module

    def record(host, status="injected"):
        return {
            "problem_name": "p4_table_entry_misconfig",
            "status": status,
            "injection_params": {"resolved_params": {"host_name": host}},
        }

    monkeypatch.setattr(
        SessionStore,
        "list_failure_injections",
        lambda *args, **kwargs: [
            record("failed", "inject_failed"),
            record("spine_1"),
            record("gateway_1"),
        ],
    )
    selected = []

    def plan(name, scenario, options, params, **kwargs):
        selected.append(params["host_name"])
        return SimpleNamespace(playbook=healthy_playbook(), root_causes=[])

    monkeypatch.setattr(module, "fault_plan", plan)
    agent = OracleAgent.__new__(OracleAgent)
    agent.session_id = "test"
    agent.scenario_name = "p4_dc_gateway"
    agent.session = SimpleNamespace(
        problem_names=["p4_table_entry_misconfig"],
        scenario_params={"backend": "kathara"},
        backend="kathara",
    )
    assert len(agent._faults()) == 1
    assert selected == ["spine_1"]


@pytest.mark.unit
def test_sonic_peer_oracle_uses_frr_shell():
    from types import SimpleNamespace
    from nika.problems.support.oracle import router_cli

    env = SimpleNamespace(
        backend="containerlab",
        machine_identities={"peer": SimpleNamespace(capabilities=("sonic",))},
    )
    step = router_cli(
        env, "peer", "Read peer adjacencies.", frr="vtysh -c 'show bgp summary'"
    )
    assert step.tool == "exec_shell"
    assert step.args["command"] == "vtysh -c 'show bgp summary'"


@pytest.mark.contract
def test_mtu_oracle_reads_sonics_routed_kernel_ports():
    from types import SimpleNamespace

    from nika.problems.rca.inventory import load_offline_net_env
    from nika.problems.registry import get_problem_class

    cls = get_problem_class("mtu_mismatch")
    problem = cls.__new__(cls)
    problem.runtime = SimpleNamespace(backend="containerlab")
    problem.net_env = load_offline_net_env(
        "isp_abilene", "s", backend="containerlab", device_profile="sonic"
    )
    playbook = problem.oracle_playbook(
        cls.Params(host_name="atlam5", intf_name="eth1", mtu=1400)
    )
    local, peer = playbook.steps
    assert local.tool == peer.tool == "sonic_exec_cli"
    assert local.args["command"] == "show interfaces status Ethernet0"
    assert local.expect("Interface Lanes Speed MTU\nEthernet0 1,2,3,4 100G 1400")
    assert not local.expect("Interface Lanes Speed MTU\nEthernet0 1,2,3,4 100G 9100")
    saved = {"mtu_local": "Ethernet0: mtu 1400"}
    assert peer.expect("Ethernet4: mtu 9100", saved)
    assert not peer.expect("Ethernet4: mtu 1400", saved)


@pytest.mark.unit
def test_routeros_frag_filter_oracle_reads_rules_before_matching_icmp():
    from nika.problems.rca.inventory import load_offline_net_env
    from nika.problems.registry import get_problem_class

    cls = get_problem_class("icmp_frag_needed_filter_misconfiguration")
    problem = cls.__new__(cls)
    problem.net_env = load_offline_net_env("dc_clos", "s", device_profile="routeros")
    (step,) = problem.oracle_playbook(cls.Params(host_name="leaf_1")).steps
    assert step.tool == "routeros_exec"
    assert step.args["command"] == "/ip firewall filter print terse"
    assert step.expect("0 chain=output action=drop protocol=icmp icmp-options=3:4")
    assert not step.expect(
        "0 chain=output action=accept protocol=icmp icmp-options=3:4"
    )


@pytest.mark.contract
def test_confusable_failures_are_registered_and_symmetric() -> None:
    """Every failure names its look-alikes for the Oracle negative control."""
    from nika.problems.registry import get_problem_class

    names = set(list_avail_problem_instances())
    for name in names:
        neighbors = get_problem_class(name).CONFUSABLE_WITH
        assert neighbors, name
        for other in neighbors:
            assert other in names and other != name, (name, other)
            assert name in get_problem_class(other).CONFUSABLE_WITH, (name, other)


@pytest.mark.unit
@pytest.mark.parametrize(
    "sent,received,confirmed",
    [
        (500, 500, False),
        (500, 490, True),
        (500, 350, False),
        (80, 70, False),
        (500, 499, False),
    ],
)
def test_silent_loss_requires_missing_transmitted_packets(sent, received, confirmed):
    from nika.agents.oracle.plan import fault_plan

    playbook = fault_plan(
        "silent_egress_packet_loss",
        "p4_dc_gateway",
        {"backend": "kathara", "topo_size": "s"},
        {"host_name": "gateway_1", "intf_name": "eth1", "bmv2_port": 2},
    ).playbook
    output = json.dumps(
        {
            "observed": sent,
            "delivered": received,
            "missing": sent - received,
            "max_queue_occupancy": 0,
        }
    )
    assert playbook.steps[-1].expect(output) is confirmed


@pytest.mark.unit
def test_containerlab_host_interface_uses_host_read_and_router_flap_uses_bounded_probes():
    from types import SimpleNamespace
    from nika.problems.support.oracle import interface_read
    from nika.problems.rca.inventory import load_offline_net_env
    from nika.problems.link_interface.link import LinkFlap
    from nika.problems.support.oracle_access import device_step

    env = load_offline_net_env(
        "dc_clos", "s", backend="containerlab", device_profile="nokia_srlinux"
    )
    host = interface_read(env, "client_1_1", "eth1", "Read the host interface.")
    assert host.tool == "exec_shell"
    assert host.args["command"] == "ip -d -s link show dev eth1"

    problem = LinkFlap(None)
    problem.net_env = load_offline_net_env("k8s_lab", "s")
    problem.runtime = SimpleNamespace(backend="kathara")
    playbook = problem.oracle_playbook(
        LinkFlap.Params(host_name="as1r1", intf_name="eth0")
    )
    probe = playbook.steps[-1]
    args = probe.args({"flap_peer": "inet 192.0.2.1/24"})
    from dataclasses import replace

    resolved = device_step(replace(probe, args=args), problem.net_env)
    assert resolved.tool == "device_read"
    assert resolved.args["commands"] == ["ping -n -c 60 -i 0.1 -W 0.1 192.0.2.1"]
    assert resolved.attempts >= 3
    assert not probe.expect("60 packets transmitted, 0 received")
    replies = "\n".join(
        f"64 bytes from 192.0.2.1: icmp_seq={i} ttl=64 time=1 ms"
        for i in [1, 2, 10, 11, 20, 21]
    )
    assert probe.expect(replies + "\n60 packets transmitted, 6 received")


@pytest.mark.unit
def test_redundant_core_link_probes_use_hosts_on_opposite_sides():
    from nika.problems.link_interface.link import _hosts_across
    from nika.problems.rca.inventory import load_offline_net_env

    env = load_offline_net_env("campus_lan", "s", device_profile="iosxr")
    # Equidistant services on the third core must not supply both endpoints.
    assert _hosts_across(env, "router_core_1", "eth0") == (
        "pc_1_1_1_1",
        "pc_2_1_1_1",
    )
    env = load_offline_net_env("p4_dc_fabric", "s")
    left, right = _hosts_across(env, "leaf_1", "eth3")
    assert left.startswith(("client_1_", "web_1"))
    assert not right.startswith(("client_1_", "web_1"))


@pytest.mark.unit
def test_bgp_evidence_excludes_adjacent_static_routers():
    from nika.problems.routing_control_plane.bgp import _oracle_bgp_peers
    from nika.problems.rca.inventory import load_offline_net_env

    env = load_offline_net_env("k8s_lab", "s")
    peers = _oracle_bgp_peers(env, "leaf_2_1")
    assert peers and "dc_exit" not in peers
    assert all("bgp" in env.machine_identities[peer].capabilities for peer in peers)


@pytest.mark.contract
def test_incorrect_resolver_oracle_uses_declared_dns_and_live_address():
    from nika.problems.registry import get_problem_instance

    problem = get_problem_instance(["host_incorrect_dns"], "dc_clos", topo_size="s")
    params = problem.resolve_params({"host_name": "client_1_1"})
    playbook = problem.oracle_playbook(params)
    server_check = next(
        step for step in playbook.steps if "lab DNS server" in step.purpose
    )
    assert server_check.evidence
    assert server_check.args["host_name"] == "web_1"
    assert "web-1.fabric" in server_check.args["command"]
    resolver_check = next(
        step for step in playbook.steps if "live addresses" in step.purpose
    )
    assert resolver_check.evidence
    observations = {"dns_address_web_1": "eth0 UP 172.100.0.2/24\neth1 UP 10.0.1.11/24"}
    assert not resolver_check.expect("nameserver 10.0.1.11\n", observations)
    assert resolver_check.expect("nameserver 10.0.1.12\n", observations)
    assert not resolver_check.expect("nameserver 10.0.1.12\n", {})
    assert not resolver_check.expect("")


@pytest.mark.contract
def test_p4_capacity_oracle_discriminates_disabled_ecn():
    from types import SimpleNamespace

    from nika.problems.rca.inventory import load_offline_net_env
    from nika.problems.registry import get_problem_class

    cls = get_problem_class("link_capacity_bottleneck")
    problem = cls.__new__(cls)
    problem.runtime = SimpleNamespace(backend="kathara")
    problem.net_env = load_offline_net_env("p4_dc_gateway", "l")
    port = next(p for p in problem.net_env.model.ports["spine_1"] if p.role == "leaf")
    playbook = problem.oracle_playbook(
        cls.Params(host_name="spine_1", intf_name=port.name)
    )
    step = next(s for s in playbook.steps if s.tool == "p4rt_exec")

    def table(threshold):
        return json.dumps(
            {
                "tables": {
                    "ecn_config": [
                        {
                            "match": {"egress_port": port.bmv2_port},
                            "params": {"threshold": threshold},
                        }
                    ]
                }
            }
        )

    assert step.expect(table(32))
    assert not step.expect(table(1024))
    assert not step.expect(table(0))
    assert not step.expect('{"tables": {"ecn_config": []}}')


@pytest.mark.unit
@pytest.mark.parametrize("reverse", [False, True])
def test_flow_oracle_selects_topology_service_address(reverse):
    from types import SimpleNamespace
    from nika.problems.forwarding_encapsulation_policy.flow_blackhole import (
        _flow_evidence_steps,
    )

    env = SimpleNamespace(backend="kathara")
    address, probe = _flow_evidence_steps(env, "client", "web", 80, "server")
    lines = [
        "4: eth1 inet 172.28.0.16/16 scope global eth1",
        "3: eth0 inet 10.0.2.11/24 scope global eth0",
    ]
    if reverse:
        lines.reverse()
    assert "dev eth0" in address.args["command"]
    command = probe.args({"server": "\n".join(lines)})["command"]
    assert "10.0.2.11" in command
    assert "172.28.0.16" not in command
    with pytest.raises(ValueError):
        probe.args({"server": lines[0] if "eth1" in lines[0] else lines[1]})


@pytest.mark.unit
def test_service_address_rejects_telemetry_on_data_interface():
    from nika.problems.support.oracle import service_address

    with pytest.raises(ValueError):
        service_address("4: eth0 inet 172.28.0.16/16 scope global eth0", "eth0")
