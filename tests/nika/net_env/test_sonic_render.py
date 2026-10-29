"""SONiC-VS rendering keeps an FRR design's routing in FRR and its addressing in CONFIG_DB."""

import json

import pytest

from nika.net_env.utils.sonic.common import container_iface, sonic_port
from nika.net_env.utils.sonic.render import node_files, translate_frr_design

pytestmark = pytest.mark.unit

DESIGN = """\
frr version 10.5.3
frr defaults traditional
hostname r1
!
interface lo
 ip address 10.255.0.1/32
 ip router isis CORE
!
interface eth0
 ip address 10.0.0.0/31
 ip router isis CORE
 isis metric 10
!
interface lo1
 ip address 198.51.100.1/24
!
router isis CORE
 net 49.0001.0000.0aff.0001.00
!
router bgp 65001
 neighbor 10.255.0.2 remote-as 65001
 neighbor 10.255.0.2 update-source lo
!
log file /var/log/frr/frr.log
"""


def rename(iface: str) -> str:
    loopbacks = {"lo": "Loopback0", "lo1": "Loopback1"}
    return loopbacks.get(iface) or sonic_port(f"eth{int(iface[3:]) + 1}")


def test_container_interfaces_map_to_sonic_ports():
    assert sonic_port("eth1") == "Ethernet0"
    assert sonic_port("eth3") == "Ethernet8"
    assert container_iface("Ethernet8") == "eth3"
    with pytest.raises(ValueError):
        sonic_port("eth0")  # management
    with pytest.raises(ValueError):
        container_iface("Ethernet2")  # not a port boundary


def test_addresses_move_to_config_db_and_routing_stays_in_frr():
    router = translate_frr_design("r1", DESIGN, rename, mtu={"eth0": 1476})
    assert "ip address" not in router.frr_conf
    assert (
        "interface Ethernet0\n ip router isis CORE\n isis metric 10" in router.frr_conf
    )
    assert "update-source Loopback0" in router.frr_conf
    assert "fpm address 127.0.0.1" in router.frr_conf
    assert "log file" not in router.frr_conf
    assert router.daemons == ("bgpd", "isisd")

    files = node_files(router)
    db = json.loads(files["config_db.json"])
    assert db["INTERFACE"] == {"Ethernet0": {}, "Ethernet0|10.0.0.0/31": {}}
    assert db["LOOPBACK_INTERFACE"] == {
        "Loopback0": {},
        "Loopback0|10.255.0.1/32": {},
        "Loopback1": {},
        "Loopback1|198.51.100.1/24": {},
    }
    assert db["PORT"]["Ethernet0"]["mtu"] == "1476"
    assert db["DEVICE_METADATA"]["localhost"]["docker_routing_config_mode"] == "split"
    assert files["lanemap.ini"] == "eth1:1,2,3,4\n"
