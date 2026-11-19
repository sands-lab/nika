"""BGP evidence must distinguish authentication, TTL, policy, and prefix faults."""

from types import SimpleNamespace

import pytest

from nika.problems.routing_control_plane import bgp

pytestmark = pytest.mark.unit


def test_frr_asn_reset_reason_read_uses_an_observed_neighbor(monkeypatch):
    problem = _problem(monkeypatch, bgp.BGPAsnMisconfig, "frr")
    step = problem.oracle_playbook(bgp.BGPAsnMisconfigParams(host_name="r1")).steps[1]
    outputs = {
        "host_bgp": "BGP router identifier 10.0.0.1, local AS number 65101\n"
        "Neighbor V AS MsgRcvd MsgSent TblVer InQ OutQ Up/Down State/PfxRcd\n"
        "r2(10.0.0.2) 4 65002 0 0 0 0 0 never Active\n"
    }
    assert step.args(outputs) == {
        "host_name": "r1",
        "command": bgp.vtysh("show bgp neighbors 10.0.0.2"),
    }
    with pytest.raises(ValueError, match="No observed BGP session"):
        step.args({"host_bgp": "BGP router identifier 10.0.0.1, local AS number 65101"})


def test_xr_bgp_acl_reference_parses_live_peer_states():
    import re
    from pathlib import Path
    from nika.problems.forwarding_encapsulation_policy.acl import BGPAclBlock
    from nika.problems.rca.inventory import load_offline_net_env

    problem = BGPAclBlock.__new__(BGPAclBlock)
    problem.net_env = load_offline_net_env("dc_clos", "s", device_profile="iosxr")
    plan = problem.oracle_playbook(problem.Params(host_name="spine_1"))
    check = next(
        step.expect for step in plan.steps if step.args["command"] == "show bgp summary"
    )
    fixtures = Path(__file__).resolve().parents[2] / "fixtures/iosxr"
    down = (fixtures / "bgp_acl_all_down.txt").read_text()
    healthy = (fixtures / "bgp_acl_healthy.txt").read_text()
    assert check(down)
    assert not check(healthy)
    mixed = re.sub(r"(?m)(^172\.16\.0\.0\s+.*\s)Active$", r"\g<1>0", down, count=1)
    assert mixed != down
    assert not check(mixed)
    assert not check("")
    assert not check("% Invalid input")
    assert not check("BGP table state: Active")


@pytest.mark.parametrize("profile", ["frr", "nokia_srlinux"])
def test_clos_hijack_requires_live_covering_prefix_owner(profile):
    import json
    from nika.problems.rca.inventory import load_offline_net_env
    from nika.problems.security.bgp import BGPHijacking

    problem = BGPHijacking.__new__(BGPHijacking)
    problem.net_env = load_offline_net_env(
        "dc_clos",
        "s",
        backend="containerlab" if profile == "nokia_srlinux" else "kathara",
        device_profile=profile,
    )
    params = problem.Params(
        host_name="leaf_1", target_network="10.0.3.0/25", symptom_host="leaf_2"
    )
    assert problem._owned_prefixes(params.target_network, params.host_name) == {
        "leaf_3": "10.0.3.0/24"
    }
    plan = problem.oracle_playbook(params)
    step = plan.steps[-1]
    assert "leaf_3" in str(step.args)
    if profile == "frr":
        assert "10.0.3.0/24" in str(step.args)
        assert step.expect("Local\n Origin IGP, valid, sourced, local, best")
        assert not step.expect("% Network not in table")
    else:

        def routes(prefix, kind="local", active=True):
            return json.dumps(
                {
                    "route": [
                        {"ipv4-prefix": prefix, "route-type": kind, "active": active}
                    ]
                }
            )

        assert step.expect(routes("10.0.3.0/24"))
        assert not step.expect(routes("10.0.3.0/25"))
        assert not step.expect(routes("10.0.3.0/24", "bgp"))
        assert not step.expect(routes("10.0.3.0/24", active=False))
        assert problem._node_asns(["leaf_1"]) != problem._node_asns(["leaf_3"])


def test_srl_hijack_reads_the_selected_path_and_referenced_attributes():
    import json
    from nika.problems.rca.inventory import load_offline_net_env
    from nika.problems.security.bgp import BGPHijacking

    problem = BGPHijacking.__new__(BGPHijacking)
    problem.net_env = load_offline_net_env(
        "dc_clos", "s", backend="containerlab", device_profile="nokia_srlinux"
    )
    plan = problem.oracle_playbook(
        problem.Params(
            host_name="leaf_1", target_network="10.0.3.0/25", symptom_host="leaf_2"
        )
    )
    path_step = next(step for step in plan.steps if step.save_as == "observer_paths")
    attr_step = next(step for step in plan.steps if callable(step.args))

    def wrapped(rib):
        return json.dumps({"network-instance": [{"name": "default", "bgp-rib": rib}]})

    def routes(best):
        return wrapped(
            {
                "afi-safi": [
                    {
                        "afi-safi-name": "ipv4-unicast",
                        "ipv4-unicast": {
                            "local-rib": {
                                "route": [
                                    {
                                        "prefix": "10.0.3.0/25",
                                        "best-route": best,
                                        "attr-id": "12",
                                    },
                                    {
                                        "prefix": "10.0.3.0/25",
                                        "best-route": False,
                                        "attr-id": "13",
                                    },
                                ]
                            }
                        },
                    }
                ]
            }
        )

    def attributes(origin):
        return wrapped(
            {
                "attr-sets": {
                    "attr-set": [
                        {
                            "index": "12",
                            "as-path": {
                                "segment": [
                                    {
                                        "as-path-index": 0,
                                        "type": "as-sequence",
                                        "member": [65001, origin],
                                    }
                                ]
                            },
                        }
                    ]
                }
            }
        )

    output = routes(True)
    assert path_step.expect(output)
    assert (
        "route 10.0.3.0/25 neighbor * origin-protocol * path-id *"
        in path_step.args["command"]
    )
    args = attr_step.args({"observer_paths": output})
    assert "attr-set 12 | as json" in args["command"]
    assert attr_step.expect(attributes(65003), {"observer_paths": output})
    assert not attr_step.expect(attributes(65005), {"observer_paths": output})
    assert not path_step.expect(routes(False))
    with pytest.raises(ValueError, match="No live best"):
        attr_step.args({"observer_paths": routes(False)})


def test_hijack_ownership_keeps_inventory_origins_without_a_unique_origin_sketch():
    from nika.problems.security.bgp import BGPHijacking

    def sketch():
        raise ValueError("The BGP sketch requires one origin per prefix")

    problem = BGPHijacking.__new__(BGPHijacking)
    problem.net_env = SimpleNamespace(
        inventory={
            "bgp": {
                "originated": [
                    {"device": "owner1", "prefix": "198.51.100.0/24"},
                    {"device": "owner2", "prefix": "198.51.100.0/24"},
                ]
            }
        },
        bgp_origin_prefixes=sketch,
    )
    assert problem._owned_prefixes("198.51.100.0/25", "hijacker") == {
        "owner1": "198.51.100.0/24",
        "owner2": "198.51.100.0/24",
    }


def _problem(monkeypatch, cls, profile):
    problem = cls.__new__(cls)
    problem.net_env = SimpleNamespace(
        machine_identities={
            host: SimpleNamespace(capabilities=(profile,)) for host in ("r1", "r2")
        }
    )
    monkeypatch.setattr(bgp, "_bgp_peers", lambda _env, _host: ["r2"])
    return problem


FRR = "router bgp 65001\n neighbor 10.0.0.2 remote-as 65002\n"
XR = "router bgp 65001\n neighbor 10.0.0.2\n  remote-as 65002\n !\n!\n"


def test_frr_asn_oracle_reads_only_the_live_session_to_the_host(monkeypatch):
    problem = _problem(monkeypatch, bgp.BGPAsnMisconfig, "frr")
    step = problem.oracle_playbook(problem.Params(host_name="r1")).steps[-1]
    outputs = {
        "host_bgp": "local AS number 65600",
        "host_addrs": "lo inet 10.255.0.1/32\neth0 inet 10.0.0.1/31",
        "peer_summary": (
            "n1(10.255.0.1) 4 65000 8 10 0 0 0 00:01:21 OpenSent 0 n1\n"
            "n2(10.255.0.3) 4 65000 9 15 3 0 0 00:03:27 0 3 n2\n"
        ),
    }
    assert step.args(outputs)["command"] == "vtysh -c 'show bgp neighbors 10.255.0.1'"
    evidence = "BGP neighbor is 10.255.0.1, remote AS 65000, local AS 65000\nBGP state = Active"
    assert step.expect(evidence, outputs)
    assert not step.expect(
        evidence.replace("remote AS 65000", "remote AS 65600"), outputs
    )
    assert not step.expect(
        evidence, {**outputs, "peer_summary": outputs["peer_summary"].splitlines()[0]}
    )
    with pytest.raises(ValueError, match="no session"):
        step.args({**outputs, "host_addrs": "lo inet 192.0.2.1/32"})


def test_rov_oracle_requires_attached_policy_and_live_cache():
    problem = object.__new__(bgp.BGPROVNotEnforced)
    problem.net_env = SimpleNamespace(
        inventory={"bgp": {"rpki": True, "rov_observer": "r1"}}
    )
    cache, policy = problem.oracle_playbook(
        bgp.BGPROVNotEnforcedParams(host_name="r1")
    ).steps
    permitted = (
        " neighbor 10.0.0.2 route-map BGP-IN in\n"
        "route-map BGP-IN permit 10\n match ip address prefix-list BUSINESS\n!\n"
    )
    assert cache.expect("cache 10.0.0.1 (connected)")
    assert not cache.expect("cache 10.0.0.1 (disconnected)")
    assert policy.expect(permitted)
    assert not policy.expect(
        permitted + "route-map BGP-IN deny 5\n match rpki invalid\n!\n"
    )
    assert not policy.expect(permitted.replace(" route-map BGP-IN in", ""))
    assert not policy.expect("")
    assert not policy.expect("% Unknown command")


def test_overlay_peers_follow_bgp_declarations():
    def peer(node, local, remote):
        return SimpleNamespace(node=node, local_asn=local, remote_asn=remote)

    env = SimpleNamespace(
        bgp_import_peers=lambda: (
            peer("branch", 65001, 65002),
            peer("hub", 65002, 65001),
            peer("other", 65003, 65002),
        )
    )
    assert bgp._oracle_bgp_peers(env, "branch") == ["hub"]


@pytest.mark.parametrize("address", ["10.0.0.2/32", "10.0.0.2@overlay"])
def test_routeros_md5_connection_selects_peer_address(address):
    assert bgp._oracle_password_peers(f"peer={address} md5=true\n") == ["10.0.0.2"]


@pytest.mark.parametrize(
    "profile,healthy,secured",
    [
        ("frr", FRR, FRR + " neighbor 10.0.0.2 password arbitrary-value\n"),
        (
            "iosxr",
            XR,
            XR.replace("  remote-as", "  password encrypted-value\n  remote-as"),
        ),
        ("routeros", "peer=10.0.0.2 md5=false\n", "peer=10.0.0.2 md5=true\n"),
    ],
)
def test_md5_requires_asymmetric_authentication(monkeypatch, profile, healthy, secured):
    problem = _problem(monkeypatch, bgp.BGPMd5PasswordMismatch, profile)
    plan = problem.oracle_playbook(bgp.BGPMd5PasswordMismatchParams(host_name="r1"))
    configured, state, peer = plan.steps
    assert configured.expect(secured)
    assert not configured.expect(healthy)
    assert peer.expect(healthy)
    assert not peer.expect(secured)
    assert not peer.expect("")
    assert not peer.expect("syntax error")
    args = state.args({"md5_config": secured})
    assert "10.0.0.2" in args["command"]
    assert "arbitrary-value" not in args["command"]
    assert state.expect("BGP state = Active")
    assert not state.expect("BGP state = Established")
    assert state.expect("authenticated-established=0\n")
    assert not state.expect("authenticated-established=1\n")
    assert state.attempts > 1
    expected_tool = {
        "frr": "exec_shell",
        "iosxr": "iosxr_exec",
        "routeros": "routeros_exec",
    }[profile]
    assert {step.tool for step in plan.steps} == {expected_tool}


@pytest.mark.parametrize("profile,healthy", [("frr", FRR), ("iosxr", XR)])
def test_ttl_requires_one_sided_gtsm(monkeypatch, profile, healthy):
    problem = _problem(monkeypatch, bgp.BGPTtlSecurityMismatch, profile)
    plan = problem.oracle_playbook(bgp.BGPTtlSecurityMismatchParams(host_name="r1"))
    configured, state, peer = plan.steps
    line = (
        " neighbor 10.0.0.2 ttl-security hops 1\n"
        if profile == "frr"
        else "  ttl-security\n"
    )
    assert configured.expect(healthy + line)
    assert not configured.expect(healthy)
    assert not configured.expect(healthy + " neighbor 10.0.0.2 ttl-security hops 255\n")
    assert peer.expect(healthy)
    assert not peer.expect(healthy + line)
    assert not peer.expect("")
    assert state.expect("BGP state = Active")
    assert not state.expect("BGP state = Established")


@pytest.mark.parametrize("profile", ["iosxr", "routeros"])
def test_policy_requires_established_session_with_missing_policy(monkeypatch, profile):
    problem = _problem(monkeypatch, bgp.BGPSessionPolicyMissing, profile)
    plan = problem.oracle_playbook(bgp.BGPSessionPolicyMissingParams(host_name="r1"))
    check = plan.steps[0].expect
    if profile == "iosxr":
        row = "10.0.0.2 0 65002 12 10 0 0 0 00:00:30 "
        assert check(row + "0!\n")
        assert not check(row + "0\n")
        assert not check(row + "Active\n")
    else:
        assert check("undefined-bgp-policies=1\n")
        assert not check("undefined-bgp-policies=0\n")
    assert not check("syntax error")


def test_routeros_asn_oracle_requires_an_independent_session(monkeypatch):
    problem = _problem(monkeypatch, bgp.BGPAsnMisconfig, "routeros")
    plan = problem.oracle_playbook(bgp.BGPAsnMisconfigParams(host_name="r1"))
    assert {step.tool for step in plan.steps} == {"routeros_exec"}
    state, peer = plan.steps[-2:]
    assert state.expect("0")
    assert not state.expect("1")
    peer.args(
        {
            "host_as": "0 name=CORE as=65601",
            "host_addrs": "0 address=172.16.0.1/31 interface=ether4",
        }
    )
    down = "0 name=peer-0 remote.address=172.16.0.1 remote.as=65001\n"
    control = "0 E name=peer-1 remote.address=172.16.0.3 remote.as=65002\n"
    assert peer.expect(down + "ESTABLISHED\n" + control)
    assert not peer.expect(down + "ESTABLISHED\n")
    assert not peer.expect(down.replace("65001", "65601") + "ESTABLISHED\n" + control)
    assert not peer.expect(
        down + "ESTABLISHED\n" + control + "1 E remote.address=172.16.0.1\n"
    )


def test_srl_asn_change_preserves_implicit_ibgp_transport_and_owned_recovery():
    from copy import deepcopy

    before = {
        "autonomous-system": 65000,
        "group": [
            {"group-name": "internal", "peer-as": 65000},
            {
                "group-name": "multihop",
                "peer-as": 65000,
                "multihop": {"admin-state": "enable", "maximum-hops": 20},
            },
        ],
        "neighbor": [
            {"peer-address": "10.0.0.1", "peer-group": "internal"},
            {
                "peer-address": "10.0.0.2",
                "peer-group": "internal",
                "multihop": {"admin-state": "disable", "maximum-hops": 5},
            },
            {"peer-address": "10.0.0.3", "peer-as": 65003},
            {"peer-address": "10.0.0.4", "peer-group": "multihop"},
            {
                "peer-address": "10.0.0.5",
                "peer-group": "internal",
                "local-as": {"as-number": 65000},
            },
        ],
    }
    commands = []

    def wrapped(config):
        return {"network-instance": [{"protocols": {"bgp": deepcopy(config)}}]}

    api = SimpleNamespace(
        srl_read=lambda *a, **kw: wrapped(before),
        srl_configure=lambda host, *lines: commands.append(lines),
    )
    problem = bgp.BGPAsnMisconfig.__new__(bgp.BGPAsnMisconfig)
    problem.net_env = SimpleNamespace(machine_identities={})
    problem.runtime = SimpleNamespace(lab_api=api, srl_get_bgp_as=lambda host: 65000)
    problem.logger = SimpleNamespace(info=lambda *a: None)
    problem._inject_asn_misconfig_containerlab(problem.Params(host_name="r1"))
    injected = "\n".join(commands[-1])
    assert "autonomous-system 65600" in injected
    for peer in ("10.0.0.1", "10.0.0.2"):
        assert f"neighbor {peer} multihop maximum-hops 64" in injected
    for peer in ("10.0.0.3", "10.0.0.4", "10.0.0.5"):
        assert peer not in injected
    after = deepcopy(before)
    after["autonomous-system"] = 65600
    for neighbor in after["neighbor"][:2]:
        neighbor["multihop"] = {"admin-state": "enable", "maximum-hops": 64}
    replies = iter([after, before, before])
    api.srl_read = lambda *a, **kw: wrapped(next(replies))
    assert problem._recover_srl_asn("r1", 65000)
    recovered = "\n".join(commands[-1])
    assert "autonomous-system 65000" in recovered
    assert "neighbor 10.0.0.2 multihop maximum-hops 5" in recovered
    assert "neighbor 10.0.0.2 multihop admin-state disable" in recovered
    for peer in ("10.0.0.3", "10.0.0.4", "10.0.0.5"):
        assert peer not in recovered
    count = len(commands)
    assert problem._recover_srl_asn("r1", 65000)
    assert len(commands) == count
