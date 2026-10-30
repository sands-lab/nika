"""Parsers that the routing failures use to read FRR output."""

import pytest

from nika.problems.routing_control_plane.bgp import bgp_neighbors_by_instance
from nika.problems.routing_control_plane.ospf import router_lsa_origins

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
