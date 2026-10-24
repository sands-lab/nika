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


def test_native_router_probes_and_linux_host_probes_use_their_own_stack() -> None:
    from types import SimpleNamespace

    plan = SimpleNamespace(nodes=(SimpleNamespace(device_name="router-a"),))
    intent = ValidationIntent(
        id="reach.router-a.host-b",
        description="Router A reaches Host B from its routing stack.",
        property="reachability",
        expected="reachable",
        source=NetworkEntity(kind="node", name="router-a", address="192.0.2.1"),
        destination=NetworkEntity(kind="node", name="host-b", address="192.0.2.2"),
        traffic=TrafficSelector(protocol="icmp"),
    )
    xr_request = _supported_request(intent, "iosxr", plan)
    ros_request = _supported_request(intent, "routeros", plan)
    assert xr_request.command == (
        "/pkg/bin/xr_cli 'ping 192.0.2.2 source 192.0.2.1 repeat 3 timeout 2'"
    )
    assert (
        "'/ping address=192.0.2.2 src-address=192.0.2.1 count=3 interval=1s'"
        in ros_request.command
    )
    host_intent = intent.model_copy(
        update={
            "source": NetworkEntity(kind="node", name="host-a", address="192.0.2.1")
        }
    )
    for platform in ("iosxr", "routeros"):
        assert _supported_request(host_intent, platform, plan).command == (
            "ping -c 3 -W 2 -I 192.0.2.1 192.0.2.2"
        )


@pytest.mark.parametrize(
    "platform,success,down",
    [
        (
            "iosxr",
            "Success rate is 100 percent (3/3)",
            "Success rate is 0 percent (0/3)",
        ),
        (
            "routeros",
            "sent=3 received=3 packet-loss=0%",
            "sent=3 received=0 packet-loss=100%",
        ),
    ],
)
def test_native_ping_distinguishes_probe_failure_from_provider_error(
    platform, success, down
) -> None:
    from nika.net_env.vendor_validation import _native_ping_status

    assert _native_ping_status(success, platform)
    assert not _native_ping_status(down, platform)
    for output in (
        "",
        "syntax error",
        "unrecognized output",
        "sent=0 received=0 packet-loss=0%",
    ):
        with pytest.raises(ValueError):
            _native_ping_status(output, platform)


def test_routeros_forwarding_hops_use_resolved_gateways_and_all_ecmp_members() -> None:
    from nika.net_env.vendor_validation import _route_next_hops

    output = """Flags: A - ACTIVE; + - ECMP
0 Ao + afi=ip4 dst-address=10.0.0.3/32 routing-table=main gateway=10.0.0.3
       immediate-gw=192.0.2.2%ether2 distance=110
1 Ao + afi=ip4 dst-address=10.0.0.3/32 routing-table=main gateway=10.0.0.3
       immediate-gw=198.51.100.2%ether3 distance=110
"""
    assert _route_next_hops(output, "routeros", "10.0.0.3/32") == {
        "192.0.2.2",
        "198.51.100.2",
    }


def test_selected_as_paths_ignore_unselected_xr_paths_and_parse_ros_as_group() -> None:
    from nika.net_env.vendor_validation import _selected_as_paths

    xr_output = """BGP routing table entry for 203.0.113.0/24
Paths: (2 available, best #2)
  Path #1: Received by speaker 0
  65002 65020
    192.0.2.2 from 192.0.2.2
      Origin IGP, valid, external
  Path #2: Received by speaker 0
  65003 65020
    192.0.2.3 from 192.0.2.3
      Origin IGP, valid, external, best, group-best
"""
    ros_output = """Flags: A - ACTIVE
0 Ab afi=ip4 dst-address=203.0.113.0/24 gateway=192.0.2.3
     immediate-gw=192.0.2.3%ether2 bgp.peer-cache-id=*B01
     .as-path="65003,65020" .origin=igp
"""
    assert _selected_as_paths(xr_output, "iosxr", "203.0.113.0/24") == [(65003, 65020)]
    assert _selected_as_paths(
        xr_output.replace(
            "65003 65020\n", "65003 65020, (Received from a RR-client)\n"
        ),
        "iosxr",
        "203.0.113.0/24",
    ) == [(65003, 65020)]
    assert _selected_as_paths(ros_output, "routeros", "203.0.113.0/24") == [
        (65003, 65020)
    ]
    with pytest.raises(ValueError, match="sequence AS paths"):
        _selected_as_paths(
            ros_output.replace("65003,65020", "{65003,65020}"),
            "routeros",
            "203.0.113.0/24",
        )
    for output in ("", "syntax error", "not a route"):
        with pytest.raises(ValueError):
            _selected_as_paths(output, "routeros", "203.0.113.0/24")


def test_bgp_session_identity_retains_source_address_and_rr_role() -> None:
    from nika.net_env.vendor_validation import _iosxr_neighbor

    output = """BGP neighbor is 10.0.0.2
 Remote AS 65000, local AS 65000, internal link
BGP state = Established, up for 00:10:00
  Route-Reflector Client
Local host: 10.0.0.1, Local port: 179
Foreign host: 10.0.0.2, Foreign port: 50511
"""
    address, peer = _iosxr_neighbor(output)
    assert address == "10.0.0.2"
    assert peer.local_address == "10.0.0.1"
    assert peer.rr_client
    assert peer.local_asn == peer.remote_asn == 65000
    ros_peer = _parse_routeros_bgp_sessions(
        "0 E remote.address=10.0.0.2 .as=65000 local.role=ibgp-rr "
        ".address=10.0.0.1 .as=65000"
    )["10.0.0.2"]
    assert ros_peer.local_address == "10.0.0.1"
    assert ros_peer.rr_client


def test_ospf_neighbor_requires_exact_peer_address_interface_and_full_state() -> None:
    from types import SimpleNamespace

    from nika.net_env.contract import AdjacencyExpectation
    from nika.net_env.vendor_validation import _ospf_established

    adjacency = AdjacencyExpectation(
        protocol="ospf",
        local_node="router-a",
        remote_node="router-b",
        local_address="192.0.2.1",
        remote_address="192.0.2.2",
        local_router_id="10.0.0.1",
        remote_router_id="10.0.0.2",
        ospf_area="0.0.0.0",
    )
    plan = SimpleNamespace(
        nodes=(
            SimpleNamespace(
                device_name="router-a",
                interfaces=(SimpleNamespace(name="eth1", address="192.0.2.1"),),
            ),
        )
    )
    xr = """Neighbors for OSPF 1
 Neighbor 10.0.0.2, interface address 192.0.2.2
    In the area 0 via interface GigabitEthernet0/0/0/1
    State is FULL, 6 state changes
"""
    ros = '0 instance=CORE area=backbone interface=ether2 address=192.0.2.2 router-id=10.0.0.2 state="Full"'
    for platform, output in (("iosxr", xr), ("routeros", ros)):
        identity = (
            {
                "instances": "0 name=CORE router-id=10.0.0.1",
                "areas": "0 name=backbone instance=CORE area-id=0.0.0.0",
            }
            if platform == "routeros"
            else {}
        )
        assert _ospf_established(output, platform, adjacency, plan, **identity)
        assert not _ospf_established(
            output.replace("192.0.2.2", "192.0.2.3"),
            platform,
            adjacency,
            plan,
            **identity,
        )
        assert not _ospf_established(
            output.replace("FULL", "INIT").replace('"Full"', '"Init"'),
            platform,
            adjacency,
            plan,
            **identity,
        )
        with pytest.raises(ValueError):
            _ospf_established(
                "unrecognized output", platform, adjacency, plan, **identity
            )

    assert not _ospf_established(
        ros,
        "routeros",
        adjacency,
        plan,
        instances="0 name=CORE router-id=10.0.0.1",
        areas="0 name=backbone instance=CORE area-id=0.0.0.1",
    )
    assert not _ospf_established(
        ros,
        "routeros",
        adjacency,
        plan,
        instances="0 name=CORE router-id=10.0.0.9",
        areas="0 name=backbone instance=CORE area-id=0.0.0.0",
    )


def test_routeros_omitted_default_ebgp_role_is_not_a_reflector_client():
    output = (
        "0 E name=peer-0 remote.address=10.0.0.3 remote.as=65001 "
        "local.address=10.0.0.2 local.as=65002 ebgp nexthop-choice=force-self\n"
    )
    peer = _parse_routeros_bgp_sessions(output)["10.0.0.3"]
    assert peer.established and peer.local_asn == 65002
    assert peer.rr_client is False


def test_routeros_ospf_reads_the_converted_port_and_designed_area() -> None:
    from nika.net_env.contract import AdjacencyExpectation
    from nika.net_env.native_routers import (
        NativeInterface,
        NativeRouterNode,
        NativeRouterPlan,
    )
    from nika.net_env.vendor_validation import _ospf_established

    adjacency = AdjacencyExpectation(
        protocol="ospf",
        local_node="dist",
        remote_node="core",
        local_address="192.0.2.1",
        remote_address="192.0.2.0",
        local_router_id="10.0.0.1",
        remote_router_id="10.0.0.2",
    )
    # A converted router's container eth0 is RouterOS ether2, in area 1.
    plan = NativeRouterPlan(
        (
            NativeRouterNode(
                "dist", (NativeInterface("eth0", "192.0.2.1", "0.0.0.1", "ether2"),)
            ),
        )
    )
    ros = '0 instance=CORE area=area-1 interface=ether2 address=192.0.2.0 router-id=10.0.0.2 state="Full"'
    identity = {
        "instances": "0 name=CORE router-id=10.0.0.1",
        "areas": "0 name=area-1 instance=CORE area-id=0.0.0.1",
    }
    assert _ospf_established(ros, "routeros", adjacency, plan, **identity)
    assert not _ospf_established(
        ros.replace("ether2", "ether1"), "routeros", adjacency, plan, **identity
    )
    assert not _ospf_established(
        ros,
        "routeros",
        adjacency,
        plan,
        instances=identity["instances"],
        areas="0 name=area-1 instance=CORE area-id=0.0.0.2",
    )


def test_routeros_isis_neighbors_parse_system_id_and_port() -> None:
    from nika.net_env.vendor_validation import _routeros_isis_adjacencies

    # Recorded RouterOS 7.21.5 ``/routing/isis/neighbor/print terse`` rows.
    output = (
        "0 D instance=CORE interface=ether2 snpa=EE:6C:89:6D:43:58 "
        "srcid=0000.0aff.0003 state=up\r\n"
        "1 D instance=CORE interface=ether3 snpa=DA:B2:5A:4C:88:73 "
        "srcid=0000.0aff.0002 state=init"
    )
    assert _routeros_isis_adjacencies(output) == {("0000.0aff.0003", "ether2")}
    with pytest.raises(ValueError):
        _routeros_isis_adjacencies("0 D instance=CORE state=up")
