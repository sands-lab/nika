"""Vendor dispatch of failure operations (no Docker)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nika.net_env.net_env_pool import get_net_env_instance
from nika.problems.support.router_ops import RouterFaultOps, router_fault_ops
from nika.runtime.base import RuntimeCapabilityError

pytestmark = pytest.mark.unit


def test_session_policy_peers_include_native_isp_inventory():
    from nika.problems.routing_control_plane.bgp import BGPSessionPolicyMissing

    env = get_net_env_instance(
        "isp_pdh", device_profile="iosxr", igp="ospf", bgp_mode="ebgp"
    )
    assert set(BGPSessionPolicyMissing.session_interfaces(env, "n10")) == {
        "10.0.0.45",
        "10.0.0.57",
        "10.0.0.59",
    }


def test_dispatch_follows_node_device_profile() -> None:
    env = get_net_env_instance("campus_lan", topo_size="s", device_profile="iosxr")
    problem = SimpleNamespace(net_env=env, runtime=object())
    ops = router_fault_ops(problem, "router_core_1")
    assert ops is not None and ops.profile == "iosxr"
    assert ops.node == "router_core_1"
    # FRR nodes keep the failure's default implementation.
    assert router_fault_ops(problem, "router_dist_1_1") is None
    frr = SimpleNamespace(net_env=get_net_env_instance("dc_clos", topo_size="s"))
    assert router_fault_ops(frr, "leaf_router_0_0") is None


def test_dc_clos_srlinux_keeps_default_failure_ops() -> None:
    """SR Linux uses its Containerlab implementation; SONiC uses FRR operations."""
    srl = get_net_env_instance(
        "dc_clos", backend="containerlab", device_profile="nokia_srlinux"
    )
    sonic = get_net_env_instance(
        "dc_clos", backend="containerlab", device_profile="sonic"
    )
    assert {"nokia_srlinux", "srl", "bgp"} <= set(
        srl.machine_identities["leaf_1"].capabilities
    )
    assert {"sonic", "bgp"} <= set(sonic.machine_identities["leaf_1"].capabilities)
    problem_srl = SimpleNamespace(net_env=srl, runtime=object())
    problem_sonic = SimpleNamespace(net_env=sonic, runtime=object())
    assert router_fault_ops(problem_srl, "leaf_1") is None
    ops = router_fault_ops(problem_sonic, "leaf_1")
    assert ops is not None and ops.profile == "sonic"


def test_unimplemented_operation_is_a_capability_error() -> None:
    with pytest.raises(RuntimeCapabilityError, match="not supported on native"):
        RouterFaultOps(object(), "r1").set_bgp_asn(65000)


@pytest.mark.parametrize("profile", ["iosxr", "routeros"])
def test_ospf_route_reads_reject_timeouts(profile):
    from nika.net_env.native_routers import native_platform

    runtime = SimpleNamespace(exec=lambda *args, **kwargs: "[TIMEOUT] command expired")
    ops = native_platform(profile).fault_ops(runtime, "r1")
    with pytest.raises(RuntimeError, match="r1"):
        ops.ospf_routes()


@pytest.mark.parametrize(
    ("record", "port", "shared", "template_id"),
    [
        ("single", "ether2", False, "*B"),
        ("multiple", "ether3", True, "*C"),
        ("networks", "ether3", True, "*D"),
        ("networks", "ether3", False, "*D"),
    ],
)
@pytest.mark.parametrize("operation", ["network_type", "timers"])
def test_routeros_ospf_template_identity_and_isolation(
    record, port, shared, template_id, operation
):
    from pathlib import Path

    from nika.net_env.utils.routeros.fault_ops import RouterOsFaultOps

    # RouterOS 7.21.5 output, including find IDs and terse rows, recorded in a lab.
    terse = (
        Path(__file__).parent / "fixtures" / f"routeros_ospf_{record}.terse"
    ).read_text()
    writes = []

    def execute(node, command, **kwargs):
        if ":foreach templateId" in command:
            return terse
        if "/ip/address/print terse where" in command:
            return f"0 address=10.0.0.3/31 network=10.0.0.2 interface={port}\n"
        if "/ip/address/print terse" in command:
            other = "ether2" if shared else port
            return (
                f"0 address=10.0.0.1/31 network=10.0.0.0 interface={other}\n"
                f"1 address=10.0.0.3/31 network=10.0.0.2 interface={port}\n"
            )
        writes.append(command)
        return ""

    ops = RouterOsFaultOps(SimpleNamespace(exec=execute), "r1")
    assert ops.ospf_network_type(port) == "point-to-point"
    assert ops.ospf_interface_timers(port) == (10, 40)
    change = (
        (lambda: ops.set_ospf_network_type(port, "non-broadcast"))
        if operation == "network_type"
        else lambda: ops.set_ospf_interface_timers(port, 5, 20)
    )
    if shared:
        with pytest.raises(RuntimeError, match="covers other interfaces"):
            change()
        assert writes == []
    else:
        change()
        assert len(writes) == 1
        expected = (
            "type=nbma"
            if operation == "network_type"
            else "hello-interval=5s dead-interval=20s"
        )
        assert (
            f"/routing ospf interface-template set {template_id} {expected}"
            in writes[0]
        )


def test_routeros_full_neighbor_matches_exact_port():
    from nika.net_env.utils.routeros.fault_ops import RouterOsFaultOps

    rows = "0 state=Full interface=ether20%main address=10.0.0.9\n"
    ops = RouterOsFaultOps(SimpleNamespace(exec=lambda *a, **k: rows), "r1")
    assert not ops.ospf_interface_neighbor_full("ether2")
    assert ops.ospf_interface_neighbor_full("ether20")


def test_iosxr_empty_bgp_entry_is_withdrawn():
    from nika.net_env.utils.iosxr.fault_ops import IosxrFaultOps

    # XRd 26.2.1 retains this header after withdrawing the last path.
    output = (
        "BGP routing table entry for 198.51.110.0/24\n"
        "Paths: (0 available, no best path)\n"
        "  Not advertised to any peer\n"
    )
    ops = IosxrFaultOps(SimpleNamespace(exec=lambda *a, **k: output), "r1")
    assert not ops.has_bgp_route("198.51.110.0/24")


@pytest.mark.parametrize(
    "output,expected",
    [
        ("MTU is 1514 (1500 is available to IP)", 1500),
        ("MTU is 1514 (500 is available to IP)", 500),
        ("IPv4 protocol is Down", None),
    ],
)
def test_iosxr_reads_ip_mtu_instead_of_frame_mtu(output, expected):
    from nika.net_env.utils.iosxr.fault_ops import IosxrFaultOps

    ops = IosxrFaultOps(SimpleNamespace(exec=lambda *a, **k: output), "r1")
    assert ops.interface_mtu("eth3") == expected


def test_iosxr_reads_native_interface_address():
    from nika.net_env.utils.iosxr.fault_ops import IosxrFaultOps

    commands = []

    def execute(node, command, **kwargs):
        commands.append(command)
        return "Internet address is 100.64.0.18/30\n"

    ops = IosxrFaultOps(SimpleNamespace(exec=execute), "r1")
    assert ops.interface_addresses("eth2") == ["100.64.0.18/30"]
    assert "show ipv4 interface GigabitEthernet0/0/0/2" in commands[0]


@pytest.mark.parametrize(
    "failure", ["ospf_interface_not_enabled", "ospf_hello_interval_mismatch"]
)
def test_ospf_recovery_retries_transient_observation_errors(monkeypatch, failure):
    from unittest.mock import Mock

    from nika.problems.registry import get_problem_class
    from nika.problems.routing_control_plane import ospf
    from nika.problems.support.polling import wait_until
    from nika.problems.support.router_ops import OspfNeighbor

    monkeypatch.setattr(
        ospf,
        "wait_until",
        lambda predicate, timeout, **kwargs: wait_until(
            predicate, timeout, interval_s=0
        ),
    )
    cls = get_problem_class(failure)
    problem = cls.__new__(cls)
    problem.net_env = SimpleNamespace(backend="kathara")
    params = cls.Params(host_name="r1", intf_name="eth0")
    if failure == "ospf_interface_not_enabled":
        neighbors = [OspfNeighbor("1.1.1.1", "10.0.0.2", "eth0", "Full")]
        ops = SimpleNamespace(
            restore_config=Mock(),
            ospf_neighbors=Mock(
                side_effect=[ValueError("daemon restarting"), neighbors]
            ),
        )
        monkeypatch.setattr(ospf, "native_fault_ops", lambda *_: ops)
        problem._saved_config = "saved OSPF config"
        problem._native_neighbors = {("1.1.1.1", "10.0.0.2", "eth0")}
        problem._original_routes = {"192.0.2.0/24"}
        problem._route_prefixes = lambda _: {"192.0.2.0/24"}
        reading = ops.ospf_neighbors
    else:
        problem._original_timers = (10, 40)
        problem._healthy_link = {
            "r1": {"full": True, "interface_up": True, "routes": ["192.0.2.0/24"]}
        }
        problem._set_timers = Mock()
        problem._timers = lambda *_: (10, 40)
        problem.link_health = Mock(
            side_effect=[ValueError("daemon restarting"), problem._healthy_link]
        )
        reading = problem.link_health
    assert problem.recover_fault(params)["verified"]
    assert reading.call_count == 2


@pytest.mark.parametrize("status", [0, 255])
def test_routeros_cli_separates_empty_tables_from_silent_ssh_failure(
    monkeypatch, status
):
    import subprocess

    from nika.net_env.utils.routeros import fault_ops

    monkeypatch.setattr(fault_ops, "CLI_COMMAND", f"exit {status}")

    def execute(node, command, **kwargs):
        return subprocess.run(
            ["sh", "-c", command], text=True, capture_output=True, check=True
        ).stdout

    ops = fault_ops.RouterOsFaultOps(SimpleNamespace(exec=execute), "r1")
    if status:
        with pytest.raises(RuntimeError, match="RouterOS command failed on r1"):
            ops.ospf_routes()
        with pytest.raises(RuntimeError, match="RouterOS command failed on r1"):
            ops.bgp_established_peers()
    else:
        assert ops.ospf_routes() == set()
        assert ops.bgp_established_peers() == set()


def test_sonic_containerlab_dispatch_uses_frr_for_fault_state():
    """Containerlab transport must not select SR Linux for a SONiC node."""
    import json

    from nika.problems.routing_control_plane.bgp import BGPMd5PasswordMismatch
    from nika.problems.routing_control_plane.isis import (
        IsisAuthenticationMismatch,
        IsisRouterLevelMismatch,
    )
    from nika.problems.routing_control_plane.ospf import OSPFDuplicateRouterId
    from nika.problems.support.ospf import ospf_health
    from nika.problems.support.oracle import router_cli
    from nika.validation.symptom.bgp_md5_probes import _received_prefixes
    from nika.validation.symptom.routing_probes import _isis_routes, _ospf_routes

    replies = {
        "show bgp summary json": {
            "ipv4Unicast": {"peers": {"10.0.0.1": {"state": "Established"}}}
        },
        "show ip ospf json": {"routerId": "10.255.0.1"},
        "show ip ospf neighbor json": {"neighbors": {}},
        "show ip route ospf json": {"10.2.0.0/24": [{"selected": True}]},
        "neighbors 10.0.0.1 routes json": {"routes": {"10.3.0.0/24": {}}},
    }

    def execute(node, command, **kwargs):
        assert node == "r1"
        if "show running-config" in command:
            return "router isis CORE\n is-type level-2-only\nexit\n!\ninterface Ethernet0\n ip router isis CORE\nexit\n"
        if "ip -4 route show proto isis" in command:
            return "10.4.0.0/24 via 10.0.0.1 dev Ethernet0 proto isis\n"
        for key, value in replies.items():
            if key in command:
                return json.dumps(value)
        raise AssertionError(command)

    env = SimpleNamespace(
        backend="containerlab",
        machine_identities={"r1": SimpleNamespace(capabilities=("sonic",))},
    )
    runtime = SimpleNamespace(backend="containerlab", exec=execute)
    for cls, method, expected in [
        (BGPMd5PasswordMismatch, "_established", {"10.0.0.1"}),
        (IsisRouterLevelMismatch, "_level", "level-2-only"),
        (IsisAuthenticationMismatch, "_authentication", {"eth1": "none"}),
        (OSPFDuplicateRouterId, "_router_id", "10.255.0.1"),
    ]:
        problem = cls.__new__(cls)
        problem.net_env, problem.runtime = env, runtime
        assert getattr(problem, method)("r1") == expected
    assert _ospf_routes(problem, "r1") == {"10.2.0.0/24"}
    assert ospf_health(problem, "r1")["routes"] == {"10.2.0.0/24"}
    assert _isis_routes(problem, "r1") == {"10.4.0.0/24"}
    assert _received_prefixes(problem, "r1", "10.0.0.1") == {"10.3.0.0/24"}
    step = router_cli(
        env, "r1", "Read routing state.", frr="vtysh -c 'show running-config'"
    )
    assert step.tool == "exec_shell"


def test_srl_area_move_preserves_other_areas_and_ignores_yang_list_order(monkeypatch):
    from copy import deepcopy
    from unittest.mock import Mock

    from nika.problems.routing_control_plane import ospf

    instance = {
        "name": "default",
        "area": [
            {
                "area-id": "0.0.0.0",
                "interface": [
                    {"interface-name": "ethernet-1/1.0", "metric": 10},
                    {"interface-name": "ethernet-1/2.0", "metric": 20},
                ],
            },
            {"area-id": "0.0.0.66", "interface": []},
            {"area-id": "0.0.0.99", "interface": []},
        ],
    }
    api = Mock()
    api.srl_protocol_instance.return_value = instance
    api.srl_exec_cli.return_value = (
        "set / network-instance default protocols ospf instance default "
        "area 0.0.0.0 interface ethernet-1/1.0 metric 10"
    )
    problem = ospf.OSPFAreaMisconfig(None)
    problem.net_env = SimpleNamespace(backend="containerlab")
    problem.runtime = SimpleNamespace(backend="containerlab", lab_api=api)
    monkeypatch.setattr(ospf, "srl_cli", lambda *_: True)
    monkeypatch.setattr(ospf, "ospf_health", lambda *_: {})
    params = problem.Params(host_name="r1")
    assert not problem.verify_fault(params)["verified"]
    problem.inject_fault(params)
    original, wrong = problem._area_move
    assert original == "0.0.0.0"
    assert wrong not in {area["area-id"] for area in instance["area"]}
    moved = deepcopy(instance)
    moved["area"][0]["area-id"] = wrong
    moved["area"][0]["interface"].reverse()
    moved["area"].reverse()
    api.srl_protocol_instance.return_value = moved
    assert problem.verify_fault(params)["verified"]
    moved["area"][-1]["interface"][0]["metric"] = 99
    assert not problem.verify_fault(params)["verified"]
    restored = deepcopy(instance)
    restored["area"][0]["interface"].reverse()
    restored["area"].reverse()
    api.srl_protocol_instance.return_value = restored
    assert problem.recover_fault(params)["verified"]


@pytest.mark.parametrize("configured_mtu", [None, 1200, 1500])
def test_xr_mtu_recovery_preserves_explicit_configuration(monkeypatch, configured_mtu):
    import json

    from nika.problems.forwarding_encapsulation_policy import mtu

    class RouterConfig:
        profile = "iosxr"

        def __init__(self):
            self.mtu = configured_mtu

        def interface_mtu(self, interface):
            return self.mtu if self.mtu is not None else 1500

        def set_interface_mtu(self, interface, value):
            self.mtu = value

        def save_config(self):
            return json.dumps({"ipv4 mtu": self.mtu})

        def restore_config(self, saved):
            self.mtu = json.loads(saved)["ipv4 mtu"]

    router = RouterConfig()
    monkeypatch.setattr(mtu, "native_fault_ops", lambda *_: router)
    problem = mtu.MtuMismatch(None)
    problem.net_env = SimpleNamespace(backend="kathara")
    problem.runtime = SimpleNamespace(backend="kathara")
    params = problem.Params(host_name="r1", intf_name="eth0")
    before = router.save_config()
    problem.inject_fault(params)
    assert router.interface_mtu("eth0") == 500
    assert problem.recover_fault(params)["verified"]
    assert router.save_config() == before
    assert problem.recover_fault(params)["verified"]
    assert router.save_config() == before


@pytest.mark.parametrize("profile", ["iosxr", "routeros"])
def test_bgp_policy_scope_and_recovery_preserve_other_session(monkeypatch, profile):
    from nika.net_env.native_routers import native_platform

    selected, other = "172.16.0.1", "172.16.0.3"
    ops = native_platform(profile).fault_ops(object(), "leaf_1")
    writes = []
    if profile == "iosxr":
        monkeypatch.setattr(ops, "_show", lambda _: "")
        original = ["route-policy PASS in", "route-policy PASS out"]
        bindings = {selected: original.copy(), other: original.copy()}
        monkeypatch.setattr(ops, "_ebgp_policies", lambda: bindings)
        monkeypatch.setattr(ops, "bgp_asn", lambda: 65003)

        def apply(*lines):
            writes.append(lines)
            neighbor = next(
                line.strip().split()[1]
                for line in lines
                if line.startswith(" neighbor ")
            )
            bindings[neighbor] = (
                []
                if any("no route-policy" in line for line in lines)
                else original.copy()
            )

        monkeypatch.setattr(ops, "_apply", apply)
        saved = (
            "router bgp 65003\n neighbor "
            + selected
            + "\n  address-family ipv4 unicast\n   route-policy PASS in\n   route-policy PASS out\n  !\n !\n!\n"
        )

        def other_state():
            return bindings[other]
    else:
        bindings = [
            {
                "name": "peer1",
                "remote.address": selected,
                "local.role": "ebgp",
                "input.filter": "PASS-IN",
                "output.filter-chain": "PASS-OUT",
            },
            {
                "name": "peer2",
                "remote.address": other,
                "local.role": "ebgp",
                "input.filter": "PASS-IN",
                "output.filter-chain": "PASS-OUT",
            },
        ]
        monkeypatch.setattr(ops, "_rows", lambda _: bindings)

        def cli(command):
            if "count-only" in command:
                return "1" if "PASS" in command else "0"
            writes.append(command)
            if "/routing bgp session " in command:
                assert f'remote.address="{selected}"' in command
                assert other not in command
                assert command.endswith(" afi=ip")
                return ""
            assert 'name="peer1"' in command and 'name="peer2"' not in command
            bindings[0]["input.filter"] = (
                "PASS-IN" if "PASS-IN" in command else "bgp-in"
            )
            bindings[0]["output.filter-chain"] = (
                "PASS-OUT" if "PASS-OUT" in command else "bgp-out"
            )
            return ""

        monkeypatch.setattr(ops, "_cli", cli)
        saved = "/routing bgp connection add name=peer1 remote.address=172.16.0.1 input.filter=PASS-IN output.filter-chain=PASS-OUT\n"

        def other_state():
            return dict(bindings[1])

    before_other = other_state().copy()
    for _ in range(2):
        ops.drop_bgp_session_policy(selected)
        assert ops.bgp_session_policy_dropped(selected)
        assert not ops.bgp_session_policy_dropped(other)
        assert not ops.bgp_session_policy_dropped()
        assert other_state() == before_other
        assert ops.restore_bgp_session_policy(saved, selected)
        assert not ops.bgp_session_policy_dropped(selected)
        assert other_state() == before_other
        assert ops.restore_bgp_session_policy(saved, selected)
        assert not ops.bgp_session_policy_dropped(selected)
        assert other_state() == before_other


def test_routeros_policy_checks_exclude_rejected_prefixes(monkeypatch):
    from nika.net_env.native_routers import native_platform

    ops = native_platform("routeros").fault_ops(object(), "r1")

    def cli(command):
        if "/routing/bgp/session/print" in command:
            return "0 E remote.address=192.0.2.1 prefix-count=3"
        assert "where bgp and afi=ip and !filtered" in command
        return (
            "0 Ab dst-address=198.51.100.0/24 belongs-to=bgp-IP-192.0.2.1@main\n"
            "1 Ab dst-address=198.51.101.0/24 belongs-to=bgp-IP-192.0.2.2@main\n"
        )

    monkeypatch.setattr(ops, "_cli", cli)
    assert ops.bgp_received_prefix_count("192.0.2.1") == 3
    assert ops.bgp_accepted_prefix_count("192.0.2.1") == 1


@pytest.mark.parametrize("unsafe", [False, True])
def test_iosxr_policy_verification_requires_default_reject(unsafe):
    from nika.net_env.utils.iosxr.fault_ops import IosxrFaultOps

    config = (
        "router bgp 65001\n"
        + (" bgp unsafe-ebgp-policy\n" if unsafe else "")
        + " neighbor 172.16.0.4\n  remote-as 65004\n  address-family ipv4 unicast\n  !\n !\n!\n"
    )
    ops = IosxrFaultOps(SimpleNamespace(exec=lambda *a, **k: config), "spine_1")
    assert ops.bgp_session_policy_dropped("172.16.0.4") is (not unsafe)
    assert ops.bgp_session_policy_dropped() is (not unsafe)


@pytest.mark.parametrize(
    "notification,expected",
    [
        ("OPEN Message Error/Bad Peer AS", True),
        ("Cease/Other Configuration Change", False),
        (None, False),
    ],
)
def test_sonic_observer_reads_frr_notification(notification, expected):
    import json

    from nika.validation.symptom.routing_probes import _bad_peer_as, _read_view

    env = get_net_env_instance(
        "dc_clos", topo_size="s", backend="containerlab", device_profile="sonic"
    )
    output = json.dumps(
        {
            "default": {
                "10.0.0.1": {
                    "bgpState": "Active",
                    "lastNotificationReason": notification,
                }
            }
        }
    )
    runtime = SimpleNamespace(exec=lambda *a, **kw: output)
    problem = SimpleNamespace(net_env=env, runtime=runtime)
    states, notes, errors = _read_view(
        problem,
        "leaf_1",
        {"host_addresses": ["10.0.0.1"], "facing": {}, "bgp": ["spine_1"]},
    )
    key = "bgp|spine_1|10.0.0.1"
    assert states == {key: "Active"}
    assert errors == []
    assert _bad_peer_as([key], [], notes) is expected


@pytest.mark.parametrize("fresh,expected", [(False, False), (True, True)])
def test_routeros_asn_symptom_requires_a_new_open_rejection(
    monkeypatch, fresh, expected
):
    from pathlib import Path

    from nika.validation.symptom import routing_probes as probes

    raw = (
        (Path(__file__).parents[2] / "fixtures/routeros_bgp_open_errors.txt")
        .read_text()
        .splitlines()
    )
    env = get_net_env_instance("dc_clos", topo_size="s", device_profile="routeros")
    reads = 0

    def execute(node, command, **kwargs):
        nonlocal reads
        if "/log/print" in command:
            reads += 1
            return "\n".join(raw if fresh and reads > 1 else raw[:2])
        if "/session/print terse where remote.address=" in command:
            wire = b"\xff" * 16 + b"\x00\x15\x03\x02\x02"
            return f"input.procid=1 .last-notification={wire.hex()}"
        return ""

    key = "bgp|leaf_1|172.16.0.1"
    problem = SimpleNamespace(
        net_env=env,
        runtime=SimpleNamespace(exec=execute),
        root_cause_name="bgp_asn_misconfig",
        _audit_routing_plan={
            "host": "spine_1",
            "host_addresses": ["172.16.0.1"],
            "facing": {},
            "bgp": ["leaf_1"],
            "up": [key],
        },
    )
    ticks = iter(range(0, 1000, 31))
    monkeypatch.setattr(probes.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(probes.time, "sleep", lambda _: None)
    ok, details = probes.bgp_bad_peer_as(problem, SimpleNamespace(host_name="spine_1"))
    assert ok is expected, details
    assert details["details"]["peer_notes"][key]["new_open_rejections"] == (
        raw[2:] if fresh else []
    )


@pytest.mark.parametrize(
    "fresh,sent_bad_as,expected",
    [(True, True, True), (False, True, False), (True, False, False)],
)
def test_srl_asn_symptom_requires_a_new_sent_bad_as(
    monkeypatch, fresh, sent_bad_as, expected
):
    import json
    from pathlib import Path

    from nika.validation.symptom import routing_probes as probes

    data = json.loads(
        (Path(__file__).parents[2] / "fixtures/srl_bgp_open_error.json").read_text()
    )
    neighbor = data["network-instance"][0]["protocols"]["bgp"]["neighbor"][0]
    if not sent_bad_as:
        neighbor["sent-messages"]["last-notification-error-code"] = "Cease"
        neighbor["sent-messages"]["last-notification-error-subcode"] = (
            "Other Configuration Change"
        )
        neighbor["received-messages"]["last-notification-error-code"] = (
            "Open Message Error"
        )
        neighbor["received-messages"]["last-notification-error-subcode"] = "Bad Peer As"
    reads = 0

    def execute(node, command, **kwargs):
        nonlocal reads
        if "as json" not in command:
            return ""
        assert "bgp neighbor * | as json" in command
        reads += 1
        neighbor["sent-messages"]["total-notifications"] = (
            2 if fresh and reads > 1 else 1
        )
        return json.dumps(data)

    key = "bgp|frankfurt|10.255.0.1"
    problem = SimpleNamespace(
        net_env=SimpleNamespace(machine_identities={}),
        runtime=SimpleNamespace(exec=execute),
        root_cause_name="bgp_asn_misconfig",
        _audit_routing_plan={
            "host": "berlin",
            "host_addresses": ["10.255.0.1"],
            "facing": {},
            "bgp": ["frankfurt"],
            "up": [key],
            "bgp_hold_sec": 90,
            "bgp_connect_retry_sec": 10,
        },
    )
    ticks = iter(range(0, 1000, 31))
    monkeypatch.setattr(probes.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(probes.time, "sleep", lambda _: None)
    ok, details = probes.bgp_bad_peer_as(problem, SimpleNamespace(host_name="berlin"))
    assert ok is expected, details
    peer = details["details"]["peer_notes"][key]
    assert peer["hold_ms"] == 90000
    assert peer["connect_retry"] == 10


def test_routeros_address_inventory_is_stable_after_readdition():
    from nika.net_env.utils.routeros.fault_ops import RouterOsFaultOps

    addresses = ["100.64.0.17/30", "198.18.1.10/32", "198.18.1.11/32"]

    def execute(node, command, **kwargs):
        return "\n".join(
            f"{index} address={address} interface=ether4"
            for index, address in enumerate(addresses)
        )

    ops = RouterOsFaultOps(SimpleNamespace(exec=execute), "br1_edge")
    before = ops.interface_addresses("ether4")
    addresses.append(addresses.pop(1))
    assert ops.interface_addresses("ether4") == before
    addresses.pop()
    assert ops.interface_addresses("ether4") != before
