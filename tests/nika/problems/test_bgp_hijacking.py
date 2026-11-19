"""Native BGP hijack inject fails when the origination never appears."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nika.problems.security.bgp import BGPHijacking, BGPHijackingParams
from nika.problems.security.bgp import _hijack_config_state

pytestmark = pytest.mark.unit


def test_recovery_state_tracks_origination_but_preserves_operator_housekeeping():
    config = "interface lo1\n ip address 198.51.100.1/24\nexit\nrouter bgp 65001\n network 198.51.100.0/24\n"
    benign = (
        config.replace("interface lo1\n", "interface lo1\n description uplink\n")
        + "ip prefix-list REF-DOCUMENTATION seq 5 permit 192.0.2.0/24\n"
    )
    assert _hijack_config_state(config, "203.0.113.1/32", None) == _hijack_config_state(
        benign, "203.0.113.1/32", None
    )
    leftover = benign + " network 203.0.113.1/32\n"
    assert _hijack_config_state(config, "203.0.113.1/32", None) != _hijack_config_state(
        leftover, "203.0.113.1/32", None
    )


def test_native_inject_fails_when_prefix_is_never_advertised(monkeypatch) -> None:
    ops = MagicMock()
    ops.profile = "sonic"
    ops.save_config.return_value = "baseline"
    ops.bgp_advertised_to.return_value = []
    monkeypatch.setattr(
        "nika.problems.security.bgp.native_fault_ops", lambda *a, **k: ops
    )
    monkeypatch.setattr("nika.problems.security.bgp.wait_until", lambda *a, **k: False)
    problem = BGPHijacking(None)
    problem.runtime = SimpleNamespace(backend="containerlab")
    problem.net_env = SimpleNamespace(
        backend="containerlab",
        machine_identities={"stuttgart": SimpleNamespace(capabilities=["sonic"])},
    )
    params = BGPHijackingParams(host_name="stuttgart", target_network="203.0.113.0/24")
    with pytest.raises(RuntimeError, match="did not advertise hijacked prefix"):
        problem.inject_fault(params)
    ops.originate_bgp_prefix.assert_called_once_with("203.0.113.0/24")
    monkeypatch.setattr(
        "nika.problems.security.bgp.native_fault_ops",
        lambda *a, **k: pytest.fail("Recovery lost the injecting provider's state"),
    )
    assert problem.recover_fault(params)["verified"]
    ops.restore_config.assert_called_once_with("baseline")


@pytest.mark.parametrize(
    "qualifier,expected", [("", [("CUSTOM", 3)]), (" le 32", []), (" ge 25 le 28", [])]
)
def test_hijack_discovers_bound_permissions_and_keeps_occupied_sequences(
    qualifier, expected
):
    from nika.problems.security.bgp import hijack_prefix_permissions

    config = f"""router bgp 65001
 neighbor 192.0.2.1 route-map OUTER out
!
route-map OUTER permit 10
 call EXPORT
!
route-map EXPORT permit 10
 match ip address prefix-list CUSTOM
!
route-map UNUSED permit 10
 match ip address prefix-list UNBOUND
!
ip prefix-list CUSTOM seq 1 permit 198.51.100.0/24{qualifier}
ip prefix-list CUSTOM seq 2 permit 192.0.2.0/24
ip prefix-list UNBOUND seq 1 permit 198.51.100.0/24
"""
    assert hijack_prefix_permissions(config, "198.51.100.0/25") == expected
    # The exact entry already admits the hijack on the next injection.
    assert (
        hijack_prefix_permissions(
            config + "ip prefix-list CUSTOM seq 3 permit 198.51.100.0/25\n",
            "198.51.100.0/25",
        )
        == []
    )


def test_hijack_does_not_extend_deny_policy_or_unrelated_prefixes():
    from nika.problems.security.bgp import hijack_prefix_permissions

    config = """router bgp 65001
 neighbor 192.0.2.1 route-map EXPORT out
 neighbor 192.0.2.2 prefix-list DIRECT in
!
route-map EXPORT deny 5
 match ip address prefix-list GUARD
!
route-map EXPORT permit 10
 match ip address prefix-list OTHER
!
ip prefix-list GUARD seq 1 permit 198.51.100.0/24
ip prefix-list DIRECT seq 1 permit 198.51.100.0/24
ip prefix-list OTHER seq 1 permit 203.0.113.0/24
"""
    assert hijack_prefix_permissions(config, "198.51.100.0/25") == [("DIRECT", 2)]


@pytest.mark.parametrize(
    "contribution,origin,local_as,prefix,expected",
    [
        ("active", "65003", "65003", "198.51.100.0/25", True),
        ("candidate", "65003", "65003", "198.51.100.0/25", False),
        ("active", "65004", "65003", "198.51.100.0/25", False),
        ("active", "65003", "", "198.51.100.0/25", False),
        ("active", "65003", "65003", "203.0.113.0/25", False),
    ],
)
def test_routeros_oracle_compares_live_origin_and_selected_prefix(
    contribution, origin, local_as, prefix, expected, monkeypatch
):
    monkeypatch.setattr(
        "nika.problems.security.bgp.native_router_profile", lambda *a: "routeros"
    )
    problem = BGPHijacking(None)
    problem.net_env = SimpleNamespace(
        machine_identities={},
        inventory={
            "bgp": {"originated": [{"device": "owner", "prefix": "198.51.100.0/24"}]}
        },
    )
    params = BGPHijackingParams(
        host_name="origin", symptom_host="observer", target_network="198.51.100.0/25"
    )
    playbook = problem.oracle_playbook(params)
    step = next(s for s in playbook.steps if "best path" in s.purpose)
    output = (
        f" Ab afi=ip contribution={contribution} dst-address={prefix} routing-table=main "
        f'gateway=192.0.2.5 immediate-gw=192.0.2.5%ether5 bgp.as-path="65001,{origin}"'
    )
    assert (
        step.expect(output, {"hijacker_addrs": f"0 name=CORE as={local_as}"})
        is expected
    )


def test_routeros_origin_delivers_the_prefix_locally_and_restores_nat():
    from nika.net_env.utils.routeros.fault_ops import RouterOsFaultOps
    from nika.net_env.utils.routeros.common import routeros_restore_script

    ops = RouterOsFaultOps(None, "router")
    calls = []
    ops._cli = lambda command: calls.append(command) or ""
    ops._rows = lambda command: []
    ops.originate_bgp_prefix("198.51.100.0/25")
    rule = (
        "/ip firewall nat add chain=dstnat dst-address=198.51.100.0/25 action=redirect"
    )
    assert rule in calls
    saved = "/system identity set name=router\n"
    assert "/ip firewall nat remove" in routeros_restore_script(
        saved, saved + rule + "\n"
    )
