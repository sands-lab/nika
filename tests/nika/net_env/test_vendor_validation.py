"""Native IOS XR and RouterOS BGP output parser regressions."""

from __future__ import annotations

import pytest

from nika.net_env.contract import NetworkEntity, TrafficSelector, ValidationIntent
from nika.net_env.vendor_validation import (
    _parse_iosxr_bgp_summary,
    _parse_routeros_bgp_sessions,
    _supported_request,
)

pytestmark = pytest.mark.unit


def test_iosxr_summary_parses_exact_peer_and_local_asn() -> None:
    output = """\
BGP router identifier 192.0.2.1, local AS number 65001
Neighbor        Spk AS      MsgRcvd MsgSent TblVer InQ OutQ Up/Down State/PfxRcd
192.0.2.2       0   65002   31      28      0      0   0    00:10:01 4
192.0.2.3       0   65003   0       0       0      0   0    never    Active
"""

    local_asn, peers = _parse_iosxr_bgp_summary(output)

    assert local_asn == 65001
    assert peers["192.0.2.2"].remote_asn == 65002
    assert peers["192.0.2.2"].local_asn == 65001
    assert peers["192.0.2.2"].established
    assert not peers["192.0.2.3"].established


@pytest.mark.parametrize(
    "output",
    ["", "% Invalid input detected at '^' marker."],
)
def test_iosxr_summary_rejects_missing_or_invalid_provider_output(output) -> None:
    with pytest.raises(ValueError, match="IOS XR"):
        _parse_iosxr_bgp_summary(output)


def test_routeros_terse_session_parses_remote_and_local_asns() -> None:
    output = (
        "0 E name=peer-a instance=default remote.address=192.0.2.2 "
        ".as=65002 .id=192.0.2.2 local.role=ebgp .address=192.0.2.1 "
        ".as=65001 .id=192.0.2.1 uptime=1d\n"
        "1 E name=peer-b instance=default remote.address=192.0.2.3 "
        "remote.as=65003 local.role=ebgp .address=192.0.2.1 local.as=65001 uptime=1d"
    )

    peers = _parse_routeros_bgp_sessions(output)

    assert peers["192.0.2.2"].remote_asn == 65002
    assert peers["192.0.2.2"].local_asn == 65001
    assert peers["192.0.2.2"].established
    assert peers["192.0.2.3"].remote_asn == 65003
    assert peers["192.0.2.3"].local_asn == 65001


def test_routeros_established_filter_allows_a_valid_empty_result() -> None:
    assert _parse_routeros_bgp_sessions("") == {}
    assert _parse_routeros_bgp_sessions("Flags: E - ESTABLISHED\n") == {}


@pytest.mark.parametrize(
    "output",
    ["syntax error (line 1 column 20)", "unrecognized router output"],
)
def test_routeros_session_parser_rejects_invalid_provider_output(output) -> None:
    with pytest.raises(ValueError, match="RouterOS"):
        _parse_routeros_bgp_sessions(output)


def test_icmp_request_binds_the_configured_source_address() -> None:
    intent = ValidationIntent(
        id="reach.host-a.host-b",
        description="Host A can reach Host B.",
        property="reachability",
        expected="reachable",
        source=NetworkEntity(kind="node", name="host-a", address="192.0.2.1"),
        destination=NetworkEntity(kind="node", name="host-b", address="192.0.2.2"),
        traffic=TrafficSelector(protocol="icmp"),
    )

    request = _supported_request(intent, "iosxr")

    assert request.command == "ping -c 3 -W 2 -I 192.0.2.1 192.0.2.2"
