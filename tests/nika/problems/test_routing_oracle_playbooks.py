"""Routing oracle evidence rejects healthy settings and neighboring fault causes."""

import json
from types import SimpleNamespace

import pytest

from nika.problems.forwarding_encapsulation_policy.acl import (
    _xr_ospf_drop_applied,
    _xr_ospf_no_adjacencies,
)
from nika.problems.routing_control_plane.isis import (
    IsisAuthenticationMismatch,
    IsisCircuitLevelMismatch,
    IsisMetricStyleMismatch,
)
from nika.problems.routing_control_plane.ospf import (
    OSPFHelloIntervalMismatch,
    OSPFNetworkTypeMismatch,
)

pytestmark = pytest.mark.unit


def _env(profile):
    intf = "e1-1" if profile == "nokia_srlinux" else "eth1"
    return SimpleNamespace(
        backend="containerlab" if profile == "nokia_srlinux" else "kathara",
        routers=["r1", "r2"],
        machine_identities={
            node: SimpleNamespace(capabilities=[profile], role="router")
            for node in ("r1", "r2")
        },
        inventory={
            "links": [
                {
                    "endpoint_a": {"device": "r1", "iface": intf},
                    "endpoint_b": {"device": "r2", "iface": intf},
                }
            ]
        },
    )


def _playbook(cls, profile, **params):
    problem = cls.__new__(cls)
    problem.net_env = _env(profile)
    return problem.oracle_playbook(cls.Params(host_name="r1", **params))


def test_srl_timer_oracle_compares_observed_interface_timers():
    def output(hello, dead):
        interface = {
            "interface-name": "ethernet-1/1.0",
            "hello-interval": hello,
            "dead-interval": dead,
        }
        instance = {
            "name": "default",
            "area": [{"area-id": "0.0.0.0", "interface": [interface]}],
        }
        ni = {"name": "default", "protocols": {"ospf": {"instance": [instance]}}}
        return json.dumps({"network-instance": [ni]})

    def evidence(local, peer):
        playbook = _playbook(
            OSPFHelloIntervalMismatch, "nokia_srlinux", intf_name="e1-1"
        )
        return _evidence(playbook, [local, peer])

    assert evidence(output(5, 20), output(10, 40))
    assert not evidence(output(10, 40), output(10, 40))


def _evidence(playbook, outputs):
    saved = {}
    evidence = []
    for step, output in zip(playbook.steps, outputs, strict=True):
        if callable(step.args):
            try:
                step.args(saved)
            except ValueError:
                if step.evidence:
                    evidence.append(False)
                continue
        passed = step.expect(output) if step.expect else None
        if step.evidence:
            evidence.append(passed)
        if step.save_as:
            saved[step.save_as] = output
    return bool(evidence) and all(evidence)


@pytest.mark.parametrize("profile", ["frr", "iosxr", "routeros"])
def test_ospf_timers_require_observed_disagreement(profile):
    plan = _playbook(OSPFHelloIntervalMismatch, profile, intf_name="eth1")
    local = "Timer intervals configured, Hello 5, Dead 20"
    peer = "Timer intervals configured, Hello 10, Dead 40"
    if profile == "routeros":
        local, peer = (
            "hello-interval=5s dead-interval=20s",
            "hello-interval=10s dead-interval=40s",
        )
    assert _evidence(plan, [local, peer])
    assert not _evidence(plan, [peer, peer])
    assert not _evidence(plan, ["OSPF not enabled on eth1", peer])
    assert not _evidence(plan, [local, "syntax error"])
    expected_tool = {
        "frr": "exec_shell",
        "iosxr": "iosxr_exec",
        "routeros": "routeros_exec",
    }[profile]
    assert all(s.tool == expected_tool for s in plan.steps)


@pytest.mark.parametrize("profile", ["frr", "routeros"])
def test_ospf_network_type_rejects_timer_mismatch(profile):
    def output(kind, hello, dead):
        if profile == "routeros":
            return f"network-type={kind} hello-interval={hello}s dead-interval={dead}s"
        return f"Network Type {kind}\nTimer intervals configured, Hello {hello}s, Dead {dead}s"

    plan = _playbook(OSPFNetworkTypeMismatch, profile, intf_name="eth1")
    assert _evidence(plan, [output("broadcast", 10, 40), output("ptp", 10, 40)])
    assert not _evidence(plan, [output("ptp", 10, 40), output("ptp", 10, 40)])
    assert not _evidence(plan, [output("broadcast", 5, 20), output("ptp", 10, 40)])
    assert not _evidence(plan, [output("broadcast", 10, 40), ""])


@pytest.mark.parametrize("profile", ["frr", "iosxr", "routeros"])
@pytest.mark.parametrize(
    "cls,setting,local,peer",
    [
        (IsisMetricStyleMismatch, "metric-style", "narrow", "wide"),
        (IsisCircuitLevelMismatch, "is-type", "level-1", "level-2-only"),
    ],
)
def test_isis_requires_incompatible_live_peer_settings(
    profile, cls, setting, local, peer
):
    def output(value):
        if profile == "routeros":
            key = "metric-type" if setting == "metric-style" else "levels"
            value = {"narrow": "old", "level-1": "l1", "level-2-only": "l2"}.get(
                value, value
            )
            return f"0 name=core {key}={value}"
        return f"router isis CORE\n {setting} {value}\n!\n"

    plan = _playbook(cls, profile)
    assert _evidence(plan, [output(peer), output(local), "Up"])
    assert not _evidence(plan, [output(peer), output(peer), "Up"])
    assert not _evidence(plan, ["", output(local), "Up"])
    assert not _evidence(plan, [output(local), output(local), "Up"])
    if cls is IsisMetricStyleMismatch:
        assert not _evidence(plan, [output(peer), output(local), "no adjacencies"])


@pytest.mark.parametrize("profile", ["frr", "iosxr"])
def test_isis_hello_authentication_requires_unauthenticated_peer(profile):
    def output(auth):
        if profile == "iosxr":
            return (
                "router isis CORE\n interface GigabitEthernet0/0/0/1\n"
                + ("  hello-password hmac-md5 encrypted secret\n" if auth else "")
                + " !\n!\n"
            )
        return (
            "interface eth1\n ip router isis CORE\n"
            + (" isis password md5 secret\n" if auth else "")
            + "!\nrouter isis CORE\n is-type level-2-only\n!\n"
        )

    plan = _playbook(IsisAuthenticationMismatch, profile)
    assert _evidence(plan, [output(False), output(True), ""])
    assert not _evidence(plan, [output(False), output(False), ""])
    assert not _evidence(plan, [output(True), output(True), ""])
    assert not _evidence(plan, ["% Invalid input", output(True), ""])


def test_frr_implicit_wide_metrics_require_an_isis_process():
    from nika.problems.routing_control_plane.isis import _metric_style

    wide = "router isis CORE\n is-type level-2-only\nexit\n"
    narrow = "router isis CORE\n metric-style narrow\nexit\n"
    assert _metric_style(wide) == "wide"
    assert _metric_style(narrow) == "narrow"
    assert _metric_style("router bgp 65000\nexit\n") is None
    plan = _playbook(IsisMetricStyleMismatch, "frr")
    assert _evidence(plan, [wide, narrow, "Up"])
    assert not _evidence(plan, [wide, wide, "Up"])
    assert not _evidence(plan, ["", narrow, "Up"])


@pytest.mark.parametrize("profile", ["frr", "iosxr", "routeros"])
def test_duplicate_id_requires_two_observed_routers(profile):
    from nika.problems.routing_control_plane.ospf import OSPFDuplicateRouterId

    plan = _playbook(OSPFDuplicateRouterId, profile)

    def output(rid):
        if profile == "routeros":
            return f"0 name=core version=2 router-id={rid}"
        if profile == "iosxr":
            return f'Routing Process "ospf CORE" with ID {rid}'
        return f"OSPF Routing Process, Router ID: {rid}"

    assert _evidence(plan, [output("1.1.1.1"), output("1.1.1.1")])
    assert not _evidence(plan, [output("1.1.1.1"), output("2.2.2.2")])
    assert not _evidence(plan, [output("1.1.1.1"), "no OSPF process"])


@pytest.mark.parametrize("profile", ["iosxr", "routeros"])
def test_missing_ospf_binding_requires_live_process_and_addressed_link(profile):
    from nika.problems.routing_control_plane.ospf import OSPFNeighborMissing

    plan = _playbook(OSPFNeighborMissing, profile)
    if profile == "iosxr":
        other = "GigabitEthernet0/0/0/0 is up, line protocol is up\n Area 0"
        address = "GigabitEthernet0/0/0/1 10.0.0.1 Up Up default"
        peer = "GigabitEthernet0/0/0/1 is up, line protocol is up\n Area 0"
        outputs = [other, address, peer]
    else:
        other = "0 interface=ether1 area=backbone type=ptp"
        address = "0 address=10.0.0.1/31 interface=ether2"
        peer = "0 interface=ether2 area=backbone type=ptp"
        outputs = ["0 R name=ether2", other, address, peer]
    assert _evidence(plan, outputs)
    outputs[-3] = ""
    assert not _evidence(plan, outputs)
    outputs[-3] = other + "\n" + peer
    assert not _evidence(plan, outputs)
    outputs[-3] = other
    outputs[-2] = ""
    assert not _evidence(plan, outputs)


def test_missing_ospf_binding_matches_evidence_to_the_correct_peer_platform():
    from nika.problems.routing_control_plane.ospf import OSPFNeighborMissing

    problem = OSPFNeighborMissing.__new__(OSPFNeighborMissing)
    problem.net_env = _env("iosxr")
    problem.net_env.routers.append("r3")
    problem.net_env.machine_identities["r3"] = SimpleNamespace(
        capabilities=["frr"], role="router"
    )
    problem.net_env.inventory["links"].append(
        {
            "endpoint_a": {"device": "r1", "iface": "eth2"},
            "endpoint_b": {"device": "r3", "iface": "eth1"},
        }
    )
    plan = problem.oracle_playbook(problem.Params(host_name="r1"))
    assert [s.tool for s in plan.steps] == [
        "iosxr_exec",
        "iosxr_exec",
        "iosxr_exec",
        "exec_shell",
    ]
    outputs = [
        "GigabitEthernet0/0/0/1 is up, line protocol is up\n Area 0",
        "GigabitEthernet0/0/0/2 10.0.0.1 Up Up default",
        "GigabitEthernet0/0/0/1 is up, line protocol is up\n Area 0",
        "eth1 is up\n Area 0",
    ]
    assert _evidence(plan, outputs)
    outputs[-1] = "OSPF not enabled on eth1"
    assert not _evidence(plan, outputs)


@pytest.mark.parametrize("profile", ["frr", "iosxr", "routeros"])
def test_area_suppression_requires_target_area_and_advertise_policy(profile):
    from nika.problems.routing_control_plane.ospf import OspfAreaRangeSuppression

    problem = OspfAreaRangeSuppression.__new__(OspfAreaRangeSuppression)
    problem.net_env = _env(profile)
    problem.branch = lambda params: {
        "area": "0.0.0.1",
        "prefixes": ["10.1.1.0/24"],
        "backbone_observer": "r2",
    }
    plan = problem.oracle_playbook(problem.Params(host_name="r1", area="1"))
    if profile == "routeros":

        def outputs(advertise="no", area="branch"):
            return [
                "0 name=branch area-id=0.0.0.1",
                f"0 area={area} prefix=10.1.1.0/24 advertise={advertise}",
                "",
                "",
            ]

        assert _evidence(plan, outputs())
        assert not _evidence(plan, outputs("yes"))
        assert not _evidence(plan, outputs(area="other"))
    else:

        def outputs(policy="not-advertise", area="1"):
            line = (
                f" area {area} range 10.1.1.0/24 {policy}\n"
                if profile == "frr"
                else f" area {area}\n  range 10.1.1.0/24 {policy}\n !\n"
            )
            return [f"router ospf CORE\n{line}!\n", "", ""]

        assert _evidence(plan, outputs())
        assert not _evidence(plan, outputs(""))
        assert not _evidence(plan, outputs(area="2"))


def test_ospf_acl_evidence_requires_a_drop_on_every_ospf_interface():
    config = """ipv4 access-list CONTROL-FILTER
 10 deny ospf any any
 20 permit ipv4 any any
!
interface GigabitEthernet0/0/0/0
 ipv4 access-group CONTROL-FILTER ingress
!
interface GigabitEthernet0/0/0/1
 ipv4 access-group CONTROL-FILTER ingress
!
router ospf 1
 area 0
  interface GigabitEthernet0/0/0/0
  !
  interface GigabitEthernet0/0/0/1
  !
 !
!
"""
    assert _xr_ospf_drop_applied(config)
    assert _xr_ospf_drop_applied(config.replace("deny ospf", "deny 89"))
    assert not _xr_ospf_drop_applied(config.replace("deny ospf", "permit ospf"))
    assert not _xr_ospf_drop_applied(
        config.replace(" 10 deny", " 5 permit ipv4 any any\n 10 deny")
    )
    assert not _xr_ospf_drop_applied(config.replace(" ingress", " egress"))
    assert not _xr_ospf_drop_applied(
        config.replace(
            "interface GigabitEthernet0/0/0/1\n ipv4 access-group CONTROL-FILTER ingress",
            "interface GigabitEthernet0/0/0/1\n ipv4 address 192.0.2.1 255.255.255.252",
        )
    )


def test_xr_ospf_adjacency_evidence_requires_zero_on_every_interface():
    empty = "Neighbor Count is 0, Adjacent neighbor count is 0"
    assert _xr_ospf_no_adjacencies(empty + "\n" + empty)
    assert not _xr_ospf_no_adjacencies(empty + "\nAdjacent neighbor count is 2")
    assert not _xr_ospf_no_adjacencies("")
    assert not _xr_ospf_no_adjacencies("% Invalid input")
