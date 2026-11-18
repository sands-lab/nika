"""Recovery keeps original policy options and reports failed quota restoration."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nika.problems.endpoint_application import transport
from nika.problems.routing_control_plane import bgp
from nika.problems.service_networking import gateway

pytestmark = pytest.mark.unit


def test_bgp_recovery_retains_all_vrf_network_options(monkeypatch):
    config = (
        "router bgp 65000\n"
        " neighbor 203.0.113.1 remote-as 65001\n"
        " address-family ipv4 unicast\n"
        "  network 192.0.2.0/24 route-map EXPORT\n"
        " exit-address-family\n!\n"
        "router bgp 65000 vrf blue\n"
        " address-family ipv4 unicast\n"
        "  network 198.51.100.0/24 route-map LAN\n"
        " exit-address-family\n!\n"
    )
    runtime = SimpleNamespace(backend="kathara", exec=Mock(return_value=config))
    problem = object.__new__(bgp.BGPMissingAdvertise)
    problem.runtime = runtime
    problem.scenario_name = "dc_clos"
    problem._advertise_running = config
    monkeypatch.setattr(bgp, "router_fault_ops", lambda *args: None)
    params = bgp.BGPMissingAdvertiseParams(host_name="edge")
    assert problem.recover_fault(params)["verified"]
    commands = [call.args[1] for call in runtime.exec.call_args_list]
    assert all("remote-as" not in cmd for cmd in commands)
    assert any("network 192.0.2.0/24 route-map EXPORT" in cmd for cmd in commands)
    assert any(
        "router bgp 65000 vrf blue" in cmd
        and "network 198.51.100.0/24 route-map LAN" in cmd
        for cmd in commands
    )


def test_snat_recovery_preserves_rules_added_before_and_after_injection(monkeypatch):
    old_rule = "ip protocol tcp ip saddr 10.1.40.0/24 snat to 198.18.1.10:40000-40063 # handle 4"
    new_rule = old_rule.replace("handle 4", "handle 9")
    later_rule = old_rule.replace("handle 4", "handle 15")
    runtime = SimpleNamespace(
        exec=Mock(
            side_effect=[
                old_rule,
                "",
                old_rule + "\n" + new_rule,
                old_rule + "\n" + new_rule + "\n" + later_rule,
                old_rule + "\n" + later_rule,
            ]
        )
    )
    problem = object.__new__(gateway.SnatPortPoolExhaustion)
    problem.runtime = runtime
    removed = []
    monkeypatch.setattr(gateway, "native_fault_ops", lambda *args: None)

    def checked(rt, host, cmd):
        if cmd.startswith("nft -a list chain"):
            return rt.exec(host, cmd)
        removed.append(cmd)

    monkeypatch.setattr(gateway, "checked_exec", checked)
    params = gateway.SnatPortPoolExhaustionParams(public_ip="198.18.1.10")
    gateway.SnatPortPoolExhaustion.inject_fault.__wrapped__(problem, params)
    assert problem.recover_fault(params)["verified"]
    assert removed == ["nft delete rule ip nat POSTROUTING handle 9"]


def test_receiver_quota_failure_is_reported_after_workload_cleanup(monkeypatch):
    problem = object.__new__(transport.ReceiverResourceContention)
    problem.scenario_name = "other"
    problem.runtime = object()
    problem._receiver_quota_applied = True
    problem._original_nano_cpus = 0
    problem._archive = {}
    cleanup = Mock()
    monkeypatch.setattr(
        transport,
        "set_nano_cpus",
        Mock(side_effect=RuntimeError("quota update rejected")),
    )
    monkeypatch.setattr(transport, "_remove_archive_job", cleanup)
    monkeypatch.setattr(transport, "container_commands", lambda *args: [])
    monkeypatch.setattr(transport.time, "sleep", lambda *args: None)
    params = transport.ReceiverResourceContentionParams(host_name="host")
    result = problem.recover_fault(params)
    assert result["verified"] is False
    assert result["details"]["workload_gone"] is True
    assert result["details"]["quota_error"] == "quota update rejected"
    cleanup.assert_called_once()


def test_service_recovery_requires_own_injection_snapshot():
    from nika.problems.management_orchestration_plane.sdn import (
        SDNControllerCrash,
        SDNControllerCrashParams,
    )
    from nika.problems.routing_control_plane.node_services import (
        RoutingServiceDown,
        RoutingServiceDownParams,
    )

    for cls, params in (
        (RoutingServiceDown, RoutingServiceDownParams(host_name="router")),
        (SDNControllerCrash, SDNControllerCrashParams(host_name="onos")),
    ):
        problem = object.__new__(cls)
        problem.runtime = SimpleNamespace(exec=Mock())
        assert problem.recover_fault(params)["verified"] is False
        problem.runtime.exec.assert_not_called()


def test_asn_recovery_rejects_missing_original_vrf(monkeypatch):
    default = "router bgp 65000\n neighbor 192.0.2.1 remote-as 65001\n!\n"
    original = default + "router bgp 65000 vrf blue\n redistribute connected\n!\n"
    problem = object.__new__(bgp.BGPAsnMisconfig)
    problem.runtime = SimpleNamespace(
        backend="kathara", exec=Mock(return_value=default)
    )
    problem._orig_asn = 65000
    problem._asn_running_config = original
    problem._asn_config_files = {}
    problem._running_asn = lambda host: 65000
    monkeypatch.setattr(bgp, "router_fault_ops", lambda *args: None)
    result = problem.recover_fault(bgp.BGPAsnMisconfigParams(host_name="edge"))
    assert result["verified"] is False


@pytest.mark.parametrize("rediscovery_succeeds", [False, True])
def test_controller_recovery_restores_listeners_after_policy_replay(
    monkeypatch, rediscovery_succeeds
):
    from nika.problems.management_orchestration_plane import sdn

    target = "tcp:172.31.0.100:6653"
    state = {"leaf_1": {"target": target, "is_connected": "true"}}
    problem = object.__new__(sdn.SDNControllerCrash)
    problem.runtime = object()
    problem.net_env = object()
    problem._controller_was_running = True
    problem._controller_ports = [6633]
    problem._controller_policy = ([{"deviceId": "of:1"}], [])
    problem._controller_apps = set()
    problem._controller_switch_state = {
        "leaf_1": {"target": target, "is_connected": "false"}
    }
    restored_ports = []

    def command(runtime, node, text):
        if node == "leaf_1":
            state[node]["is_connected"] = "false"

    monkeypatch.setattr(sdn, "checked_exec", command)
    monkeypatch.setattr(sdn, "wait_for_onos", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        sdn, "wait_for_of_sessions", lambda *args, **kwargs: rediscovery_succeeds
    )
    monkeypatch.setattr(sdn, "_rest_policy_present", lambda *args: False)
    monkeypatch.setattr(sdn, "_restore_rest_policy", lambda *args: True)
    monkeypatch.setattr(sdn, "_restore_onos_apps", lambda *args: True)
    monkeypatch.setattr(sdn, "_switch_controller_state", lambda *args: state)
    monkeypatch.setattr(
        sdn,
        "_tcp_listening",
        lambda runtime, host, port: (
            port != 6653 and (port != 8181 or rediscovery_succeeds)
        ),
    )
    monkeypatch.setattr(sdn, "get_openflow_listen_ports", lambda runtime: [6633])
    monkeypatch.setattr(
        sdn,
        "set_openflow_listen_ports",
        lambda runtime, ports: restored_ports.append(ports),
    )
    monkeypatch.setattr(
        sdn, "wait_until", lambda predicate, *args, **kwargs: bool(predicate())
    )
    result = problem.recover_fault(sdn.SDNControllerCrashParams(host_name="onos"))
    assert result["verified"] is rediscovery_succeeds
    assert restored_ports == [[6633, 6653], [6633]]
    assert state["leaf_1"]["is_connected"] == "false"
    assert state["leaf_1"]["target"] == target


def test_controller_recovery_replays_lost_groups_before_flows(monkeypatch):
    from nika.problems.management_orchestration_plane import sdn

    flow = {
        "deviceId": "of:1",
        "priority": 1000,
        "selector": {"criteria": []},
        "treatment": {"instructions": [{"type": "GROUP", "groupId": "1"}]},
    }
    group = {
        "deviceId": "of:1",
        "type": "SELECT",
        "appCookie": "0x1",
        "groupId": "1",
        "buckets": [],
    }
    saved = ([flow], [group])
    batches = []
    monkeypatch.setattr(sdn, "_saved_rest_policy", Mock(side_effect=[([], []), saved]))
    monkeypatch.setattr(
        sdn, "_onos_batch_resilient", lambda runtime, ops, **kwargs: batches.extend(ops)
    )
    assert sdn._restore_rest_policy(object(), saved)
    assert [path for method, path, body in batches] == [
        "/onos/v1/groups/of:1",
        "/onos/v1/flows/of:1",
    ]
    assert batches[0][2]["appCookie"] == "0x1"
    assert batches[1][2] == flow


@pytest.mark.parametrize("existing_af", [False, True])
def test_srl_recovery_removes_an_injected_neighbor_policy_override(existing_af):
    policy = "set / routing-policy policy BASE default-action policy-result accept"
    neighbor = "set / network-instance default protocols bgp neighbor 192.0.2.1 peer-group PEERS"
    original_bgp = neighbor
    if existing_af:
        original_bgp += "\nset / network-instance default protocols bgp neighbor 192.0.2.1 afi-safi ipv4-unicast admin-state enable"
    runtime = SimpleNamespace(
        backend="containerlab",
        exec=Mock(
            side_effect=[
                "set / routing-policy policy CLOS01-OUT default-action policy-result next-policy",
                "OK",
                policy,
                original_bgp,
            ]
        ),
    )
    problem = object.__new__(bgp.BGPMissingAdvertise)
    problem.runtime = runtime
    problem._advertise_srl = {
        "routing-policy": (policy,),
        "network-instance default protocols bgp": tuple(
            sorted(original_bgp.splitlines())
        ),
    }
    result = problem.recover_fault(bgp.BGPMissingAdvertiseParams(host_name="leaf1"))
    assert result["verified"] is True
    script = runtime.exec.call_args_list[1].args[1]
    remove_override = (
        "delete /network-instance default protocols bgp neighbor 192.0.2.1 afi-safi ipv4-unicast"
        + (" export-policy" if existing_af else "")
    )
    assert remove_override in script.splitlines()
    assert script.index(remove_override) < script.index(
        "delete /routing-policy policy CLOS01-OUT"
    )


def test_nat_recovery_preserves_an_unrelated_rule_with_a_shared_handle_prefix(
    monkeypatch,
):
    unrelated = "ip saddr 10.2.0.0/24 snat to 198.18.2.10 # handle 10"
    owned = "ip saddr 10.1.40.0/24 snat to 198.18.1.10 # handle 1"
    chain = unrelated + "\n" + owned
    runtime = SimpleNamespace(exec=Mock(side_effect=["", chain, "", "", "", chain]))
    problem = object.__new__(gateway.NatMappingRemovedWithoutDrain)
    problem.runtime = runtime
    restored = []
    monkeypatch.setattr(gateway, "native_fault_ops", lambda *args: None)
    monkeypatch.setattr(
        gateway.IPv4Snapshot, "capture", lambda *args: SimpleNamespace(restore=Mock())
    )
    monkeypatch.setattr(
        gateway, "checked_exec", lambda rt, host, cmd: restored.append(cmd)
    )
    params = gateway.NatMappingRemovedWithoutDrainParams(
        nat_ip_a="198.18.1.10", nat_ip_b="198.18.1.11", wan_interface="eth0"
    )
    gateway.NatMappingRemovedWithoutDrain.inject_fault.__wrapped__(problem, params)
    assert problem.recover_fault(params)["verified"]
    assert restored == [
        "nft replace rule ip nat POSTROUTING handle 1 " + owned.split(" # handle")[0]
    ]


def test_controller_already_down_does_not_require_rest_snapshots(monkeypatch):
    from nika.problems.management_orchestration_plane import sdn

    problem = object.__new__(sdn.SDNControllerCrash)
    problem.runtime = SimpleNamespace(exec=Mock())
    problem.net_env = object()
    problem.verify_fault = Mock(return_value={"verified": True})
    policy = Mock(side_effect=AssertionError("The stopped controller has no REST API"))
    monkeypatch.setattr(sdn, "_saved_rest_policy", policy)
    monkeypatch.setattr(sdn, "_switch_controller_state", lambda *args: {})
    params = sdn.SDNControllerCrashParams(host_name="onos")
    sdn.SDNControllerCrash.inject_fault.__wrapped__(problem, params)
    assert problem.recover_fault(params)["verified"]
    policy.assert_not_called()
    assert problem.runtime.exec.call_count == 1


def test_listener_recovery_preserves_an_existing_channel_block(monkeypatch):
    from nika.problems.management_orchestration_plane import sdn

    state = {"leaf_1": {"target": "tcp:172.31.0.100:6653", "is_connected": "false"}}
    problem = object.__new__(sdn.SouthboundPortMismatch)
    problem.runtime = object()
    problem.net_env = object()
    problem._original_listen_ports = [6633, 6653]
    problem._original_switch_state = state
    monkeypatch.setattr(sdn, "_switch_controller_state", lambda *args: state)
    monkeypatch.setattr(sdn, "set_openflow_listen_ports", lambda *args: None)
    monkeypatch.setattr(sdn, "get_openflow_listen_ports", lambda *args: [6633, 6653])
    monkeypatch.setattr(sdn, "_tcp_listening", lambda *args: True)
    monkeypatch.setattr(
        sdn, "wait_until", lambda predicate, *args, **kwargs: bool(predicate())
    )
    assert problem.recover_fault(sdn.SouthboundPortMismatchParams(host_name="onos"))[
        "verified"
    ]
    assert state["leaf_1"]["is_connected"] == "false"
