"""Read IPv4 prefixes separately from a neighbor's other address families."""

import pytest

pytestmark = pytest.mark.unit


def test_routeros_family_state_matches_vrf_scoped_session_addresses(monkeypatch):
    from nika.net_env.utils.routeros.fault_ops import RouterOsFaultOps

    connections = "0 name=wg-hq1 remote.address=10.31.11.1 afi=ip\n"
    sessions = (
        "0 E remote.address=10.31.12.1@overlay local.afi=ipv6 remote.afi=ip prefix-count=0\n"
        "1 E remote.address=10.31.11.1@overlay local.afi=ip remote.afi=ip prefix-count=3\n"
    )
    ops = RouterOsFaultOps(None, "router")
    monkeypatch.setattr(
        ops,
        "_cli",
        lambda command: connections if "/connection/" in command else sessions,
    )
    assert ops.bgp_ipv4_unicast_status("10.31.11.1") == {
        "configuration": "ip",
        "configured": True,
        "negotiated": True,
        "state": "Established",
        "received_prefix_count": 3,
    }
    sessions = sessions.replace("prefix-count=3", "")
    with pytest.raises(ValueError, match="Unreadable BGP prefix count"):
        ops.bgp_ipv4_unicast_status("10.31.11.1")


@pytest.mark.parametrize("peer", [None, "10.31.11.1"])
def test_oracle_confirms_missing_local_ipv4_family(peer):
    check = _oracle_check(peer)
    assert check(_observation())
    assert check(_observation().replace("afi=ipv6", "afi=ipv6,vpnv4"))


def test_oracle_rejects_missing_filter_chain_with_negotiated_ipv4():
    output = _observation().replace("afi=ipv6", "afi=ip")
    output = output.replace("afi=ip\n", "afi=ip input.filter=bgp-in\n")
    assert not _oracle_check()(output)


@pytest.mark.parametrize(
    "output",
    [
        "",
        "tool_error: timeout",
        "failure: command rejected",
        "0 name=peer remote.address=10.31.11.1 afi=ipv6",
    ],
)
def test_oracle_requires_observed_configuration_and_session(output):
    assert not _oracle_check()(output)


@pytest.mark.parametrize(
    "old,new",
    [
        (" afi=ipv6", " afi=ip"),
        (" afi=ipv6", ""),
        ("local.afi=ipv6", "local.afi=ip,ipv6"),
        ("local.afi=ipv6", ""),
        ("remote.afi=ip", "remote.afi=ipv6"),
        ("remote.afi=ip", ""),
        ("10.31.11.1@overlay", "10.31.12.1@overlay"),
    ],
)
def test_oracle_rejects_filter_policy_faults_and_incomplete_capabilities(old, new):
    assert not _oracle_check()(_observation().replace(old, new))


def test_oracle_correlates_configuration_and_session_for_selected_peer():
    output = _observation()
    assert not _oracle_check("10.31.12.1")(output)
    # Another peer's IPv4 capability cannot confirm the misconfigured connection.
    output += (
        "1 E remote.address=10.31.12.1 local.afi=ip remote.afi=ip prefix-count=0\n"
    )
    assert _oracle_check("10.31.11.1")(output)
    assert not _oracle_check("10.31.12.1")(output)
    assert not _oracle_check()(
        output + output.split("=== ESTABLISHED BGP SESSIONS ===")[1]
    )


def _observation():
    return (
        "0 name=wg-hq1 remote.address=10.31.11.1 afi=ipv6\n"
        "=== ESTABLISHED BGP SESSIONS ===\n"
        "0 E remote.address=10.31.11.1@overlay local.afi=ipv6 remote.afi=ip prefix-count=0\n"
    )


def _oracle_check(peer=None):
    from nika.problems.routing_control_plane.bgp_afi import (
        BGPAddressFamilyNotActivated,
        BGPAddressFamilyNotActivatedParams,
    )

    problem = BGPAddressFamilyNotActivated.__new__(BGPAddressFamilyNotActivated)
    playbook = problem.oracle_playbook(
        BGPAddressFamilyNotActivatedParams(host_name="router", neighbor_ip=peer)
    )
    return next(step.expect for step in playbook.steps if step.evidence)
