"""Parsers that the routing failures use to read FRR output."""

import pytest

from nika.problems.routing_control_plane.bgp import bgp_neighbors_by_instance
from nika.problems.routing_control_plane.ospf import router_lsa_origins
from nika.problems.routing_control_plane.isis import _hello_authentication
from nika.problems.routing_control_plane.vrf import _frr_imports

pytestmark = pytest.mark.unit

RUNNING_CONFIG = """\
frr version 10.5.3
!
router bgp 65000
 bgp router-id 172.16.0.0
 neighbor 172.16.0.1 remote-as 65100
 neighbor eth1 interface remote-as external
 neighbor FABRIC peer-group
 neighbor FABRIC remote-as external
 neighbor 10.0.0.9 peer-group FABRIC
 !
 address-family ipv4 unicast
  network 192.168.0.0/24
 exit-address-family
exit
!
router bgp 65000 vrf CORP
 neighbor 10.9.0.1 remote-as 65010
exit
!
router ospf
 ospf router-id 172.16.0.0
exit
"""

OSPF_DATABASE = """\

       OSPF Router with ID (10.255.0.4)

                Router Link States (Area 0.0.0.0)

Link ID         ADV Router      Age  Seq#       CkSum  Link count
10.255.0.4     10.255.0.4        20 0x8000000c 0xb06b 5
10.255.0.7     10.255.0.7        20 0x8000000a 0x56db 5

                Net Link States (Area 0.0.0.0)

Link ID         ADV Router      Age  Seq#       CkSum
10.0.0.13      10.255.0.9        30 0x80000002 0x1234

                Router Link States (Area 0.0.0.1)

Link ID         ADV Router      Age  Seq#       CkSum  Link count
172.16.0.9     172.16.0.9        51 0x80000004 0x4d1a 2
"""


def test_bgp_neighbors_by_instance_reads_every_bgp_block():
    assert bgp_neighbors_by_instance(RUNNING_CONFIG) == {
        "router bgp 65000": ["172.16.0.1", "eth1", "FABRIC"],
        "router bgp 65000 vrf CORP": ["10.9.0.1"],
    }


def test_router_lsa_origins_skips_other_lsa_types():
    assert router_lsa_origins(OSPF_DATABASE) == {
        "0.0.0.0": {"10.255.0.4", "10.255.0.7"},
        "0.0.0.1": {"172.16.0.9"},
    }


def test_isis_hello_authentication_skips_passive_and_non_isis_interfaces():
    config = """\
interface eth0
 ip router isis CORE
 isis password md5 K7m4P9r2
exit
interface eth1
 ip router isis CORE
exit
interface eth2
 ip router isis CORE
 isis password clear K7m4P9r2
exit
interface lo
 ip router isis CORE
 isis passive
exit
interface eth3
 ip address 192.0.2.1/24
exit
router isis CORE
 is-type level-2-only
exit
"""
    assert _hello_authentication(config) == {
        "eth0": "md5",
        "eth1": "none",
        "eth2": "clear",
    }


def test_vrf_imports_do_not_mix_instances_or_address_families():
    config = """\
router bgp 65000
 address-family ipv4 unicast
  import vrf vrf_corp
  import vrf vrf_server
 exit-address-family
exit
router bgp 65000 vrf vrf_corp
 address-family ipv4 unicast
  import vrf default
  import vrf vrf_server
 exit-address-family
 address-family ipv6 unicast
  import vrf vrf_iot
 exit-address-family
exit
router bgp 65000 vrf vrf_server
 address-family ipv4 unicast
  import vrf vrf_guest
  import vrf vrf_corp
 exit-address-family
exit
"""
    assert _frr_imports(config, "vrf_corp") == {"default", "vrf_server"}
    assert _frr_imports(config, "vrf_server") == {"vrf_guest", "vrf_corp"}
    assert _frr_imports(config, "vrf_guest") == set()
