"""BGP evidence must distinguish authentication, TTL, policy, and prefix faults."""

from types import SimpleNamespace

import pytest

from nika.problems.routing_control_plane import bgp

pytestmark = pytest.mark.unit


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


@pytest.mark.parametrize("profile", ["iosxr", "routeros"])
def test_max_prefix_requires_overflow_reason(monkeypatch, profile):
    problem = _problem(monkeypatch, bgp.BGPMaxPrefixExceeded, profile)
    monkeypatch.setattr(problem, "_resolve_session", lambda _: ("r1", "r2", "10.0.0.2"))
    plan = problem.oracle_playbook(
        bgp.BGPMaxPrefixExceededParams(receiver_name="r1", peer_name="r2")
    )
    check = plan.steps[1].expect
    reason = "Last reset: peer had exceeded the max. no. of prefixes configured"
    assert check("BGP state = Idle\n" + reason)
    assert not check("BGP state = Established\n" + reason)
    assert check(
        "0 limit-exceeded remote.address=10.0.0.2\nprefix-limit-established=0\n"
    )
    assert not check(
        "0 limit-exceeded remote.address=10.0.0.2\nprefix-limit-established=1\n"
    )
    assert not check(
        "Flags: L - limit-exceeded\n0 remote.address=10.0.0.2\nprefix-limit-established=0\n"
    )
    assert not check("BGP state = Idle; Maximum prefixes allowed 40")
    assert not check("Last reset: authentication failure")
