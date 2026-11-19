"""Device faults cover all data ports and require independent observations."""

from types import SimpleNamespace

import pytest

from nika.problems.registry import get_problem_class
from nika.problems.forwarding_encapsulation_policy.switch_internal_corruption import (
    _available_probe_port_start,
    device_probe_pairs,
)
from nika.net_env.net_env_pool import get_net_env_instance

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "fault", ["device_flow_blackhole", "device_forwarding_packet_corruption"]
)
@pytest.mark.parametrize("size,ports", [("s", 5), ("m", 6)])
def test_device_injection_verification_and_recovery_cover_every_data_port(
    fault, size, ports, monkeypatch
):
    problem = get_problem_class(fault)("enterprise_branch", topo_size=size)
    params = problem.parse_params({"forwarding_device": "hq_edge", "intf_name": "eth0"})
    active = set()

    class Classifier:
        def attach(self, node, intf, seed, udp_ports):
            active.add((node, intf))

        def attached(self, node, intf):
            return (node, intf) in active

        def detach(self, node, intf):
            active.discard((node, intf))

    monkeypatch.setattr(
        type(problem),
        "runtime",
        property(lambda self: SimpleNamespace(exec=lambda *args: "")),
        raising=False,
    )
    monkeypatch.setattr(problem, "_classifier", Classifier)
    problem.inject_fault(params)
    assert active == {("hq_edge", f"eth{i}") for i in range(ports)}
    assert problem.verify_fault(params)["verified"]
    active.remove(("hq_edge", "eth4"))
    assert not problem.verify_fault(params)["verified"]
    assert problem.recover_fault(params)["verified"]
    assert not active


def test_partial_device_injection_rolls_back_its_completed_ports(monkeypatch):
    problem = get_problem_class("device_flow_blackhole")(
        "enterprise_branch", topo_size="s"
    )
    params = problem.parse_params({"forwarding_device": "hq_edge", "intf_name": "eth0"})
    active = set()

    class Classifier:
        def attach(self, node, intf, seed, udp_ports):
            if intf == "eth2":
                raise RuntimeError("attachment failed")
            active.add(intf)

        def detach(self, node, intf):
            active.remove(intf)

    monkeypatch.setattr(
        type(problem),
        "runtime",
        property(lambda self: SimpleNamespace(exec=lambda *args: "")),
        raising=False,
    )
    monkeypatch.setattr(problem, "_classifier", Classifier)
    with pytest.raises(RuntimeError, match="attachment failed"):
        problem.inject_fault(params)
    assert not active


def test_a_transit_chain_cannot_localize_a_device_from_path_evidence():
    env = get_net_env_instance("enterprise_branch", topo_size="s")
    # Each branch edge has several physical ports, but every usable service
    # path shares an uplink. Port count alone is not localization evidence.
    with pytest.raises(ValueError, match="link-disjoint"):
        device_probe_pairs(env, "br1_edge")


@pytest.mark.parametrize(
    "fault", ["device_flow_blackhole", "device_forwarding_packet_corruption"]
)
def test_benchmark_validation_rejects_unlocalizable_device_targets(fault):
    from nika.problems.support.benchmark_targets import device_interfaces

    env = get_net_env_instance("enterprise_branch", topo_size="s")
    ctx = SimpleNamespace(net_env=env, ifaces_by_device=device_interfaces(env))
    cls = get_problem_class(fault)
    with pytest.raises(ValueError, match="link-disjoint"):
        cls.validate_benchmark_inject(
            ctx, {"forwarding_device": "br1_edge", "intf_name": "eth0"}
        )
    cls.validate_benchmark_inject(
        ctx, {"forwarding_device": "hq_edge", "intf_name": "eth1"}
    )


def test_background_checksum_errors_cannot_prove_corruption_of_probe_flows():
    from nika.agents.oracle.plan import fault_plan

    plan = fault_plan(
        "device_forwarding_packet_corruption",
        "enterprise_branch",
        {"topo_size": "s"},
        {"forwarding_device": "hq_edge", "intf_name": "eth1"},
    ).playbook
    evidence = [step for step in plan.steps if step.evidence]
    assert len(evidence) == 8
    for index, (transfer, counter, packets, retransmissions) in enumerate(
        zip(evidence[::4], evidence[1::4], evidence[2::4], evidence[3::4], strict=True)
    ):
        assert not transfer.expect("ok=0 failed=24 local_errors=24")
        assert transfer.expect("ok=13 failed=11 local_errors=0")
        assert transfer.expect("ok=24 failed=0 local_errors=0")
        assert not transfer.expect("ok=0 failed=0 local_errors=0")
        assert not transfer.expect("curl: command not found")
        assert packets.tool == "packet_capture_inspect"
        assert not packets.expect('{"total_available": 0}')
        assert packets.expect('{"total_available": 24}')
        key = "corrupt_1" if index else "corrupt"
        before = {key + "_retrans_before": "TcpRetransSegs 7 0.0"}
        assert retransmissions.expect("TcpRetransSegs 8 0.0", before)
        assert not retransmissions.expect("TcpRetransSegs 7 0.0", before)
        assert not retransmissions.expect("TcpRetransSegs 8 0.0", {})
    outputs = {"corrupt_ports": "", "corrupt_1_ports": ""}
    captures = [
        step.args(outputs) for step in plan.steps if step.tool == "packet_capture_start"
    ]
    assert all(
        "src portrange 24000-24023" in step["capture_filter"] for step in captures
    )


def test_corruption_probes_skip_owned_ipv4_and_ipv6_source_ports():
    assert _available_probe_port_start("") == 24000
    sockets = (
        "FIN-WAIT-1 0 65687 10.0.40.2:24000 198.51.100.10:80\n"
        "TIME-WAIT 0 0 [::ffff:10.0.40.2]:24024 [::ffff:198.51.100.10]:80\n"
    )
    assert _available_probe_port_start(sockets) == 24048


def test_corruption_probe_port_selection_rejects_unavailable_socket_state():
    with pytest.raises(ValueError, match="socket state"):
        _available_probe_port_start("ss: command not found")
    sockets = "\n".join(
        f"TIME-WAIT 0 0 10.0.40.2:{port} 198.51.100.10:80"
        for port in range(24000, 32768, 24)
    )
    with pytest.raises(RuntimeError, match="No free source-port block"):
        _available_probe_port_start(sockets)


def test_native_injection_does_not_issue_cli_commands(monkeypatch):
    from unittest.mock import MagicMock

    problem = object.__new__(get_problem_class("device_flow_blackhole"))
    problem.net_env = get_net_env_instance(
        "dc_clos", topo_size="s", backend="containerlab", device_profile="nokia_srlinux"
    )
    observed = []

    def read(node, command):
        observed.append(command)
        raise AssertionError("Native injection must not execute a device command")

    problem.runtime = SimpleNamespace(exec=read)
    injector = MagicMock()
    monkeypatch.setattr(problem, "_classifier", lambda: injector)
    params = problem.parse_params({"forwarding_device": "leaf_1", "intf_name": "e1-1"})
    problem.inject_fault(params)
    assert not observed
    assert injector.attach.call_count == 5
    assert all(call.args[-1] == set() for call in injector.attach.call_args_list)


def test_wireguard_control_ports_come_from_linux_or_the_native_design():
    cls = get_problem_class("device_flow_blackhole")
    for profile in ("frr", "routeros"):
        problem = object.__new__(cls)
        problem.net_env = get_net_env_instance(
            "enterprise_branch", topo_size="s", device_profile=profile
        )
        calls = []

        def read(node, command):
            calls.append(command)
            assert command == "wg show all listen-port"
            return "wg_br1 51820\nwg_br2 51821\n"

        problem.runtime = SimpleNamespace(exec=read)
        ports = problem._control_udp_ports("hq_edge")
        assert {51820, 51821} <= ports
        assert calls == (["wg show all listen-port"] if profile == "frr" else [])
