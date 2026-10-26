"""IOS XR and RouterOS telemetry parsers and setup scripts."""

from __future__ import annotations

import json

import pytest

from nika.telemetry import iosxr, prometheus, routeros, srl

pytestmark = pytest.mark.unit

_XR_SHOW = """\
GigabitEthernet0/0/0/0 is up, line protocol is up
  Internet address is 193.10.11.1/24
     12 packets input, 1200 bytes, 0 total input drops
     8 packets output, 800 bytes, 0 total output drops
GigabitEthernet0/0/0/1 is up, line protocol is up
  Internet address is 195.11.14.1/24
     4 packets input, 400 bytes, 0 total input drops
     2 packets output, 200 bytes, 0 total output drops
MgmtEth0/RP0/CPU0/0 is up, line protocol is up
     1 packets input, 10 bytes, 0 total input drops
     1 packets output, 10 bytes, 0 total output drops
Loopback0 is up, line protocol is up
     0 packets input, 0 bytes, 0 total input drops
     0 packets output, 0 bytes, 0 total output drops
"""

_ROS_STATS = """\
Columns: NAME, RX-BYTE, TX-BYTE, RX-PACKET, TX-PACKET, RX-DROP, TX-DROP, RX-ERROR, TX-ERROR
ether1  99  88  7  6  0  0  0  0
ether2  123  456  10  11  1  0  2  0
ether4  1  2  3  4  0  0  0  0
0 R ether2
0   ether4
"""

_ROS_BGP = """\
0 remote.address=193.10.11.2 established=yes remote.as=2
1 remote.address=10.0.0.9 established=no remote.as=9
"""


def test_parse_xr_interfaces_skips_mgmt_and_loopback() -> None:
    rows = {row["device"]: row for row in iosxr.parse_interfaces(_XR_SHOW)}
    assert set(rows) == {"GigabitEthernet0/0/0/0", "GigabitEthernet0/0/0/1"}
    assert rows["GigabitEthernet0/0/0/0"]["rx_packets"] == 12
    assert rows["GigabitEthernet0/0/0/0"]["up"] == 1
    assert rows["GigabitEthernet0/0/0/0"]["rx_drops"] == 0
    assert rows["GigabitEthernet0/0/0/1"]["tx_bytes"] == 200


_XR_BGP = """\
BGP router identifier 193.10.11.1, local AS number 1
Neighbor        Spk    AS MsgRcvd MsgSent   TblVer  InQ OutQ  Up/Down  St/PfxRcd
193.10.11.2       0     2      12      14        0    0    0 00:02:11          1
10.0.0.9          0     9       0       0        0    0    0 00:00:03       Idle
"""


def test_parse_xr_bgp_summary() -> None:
    rows = {row["neighbor"]: row for row in iosxr.parse_bgp_summary(_XR_BGP)}
    assert rows["193.10.11.2"]["up"] == 1
    assert rows["193.10.11.2"]["prefixes"] == 1
    assert rows["10.0.0.9"]["up"] == 0


def test_parse_routeros_interface_stats() -> None:
    rows = {row["device"]: row for row in routeros.parse_interface_stats(_ROS_STATS)}
    assert rows["ether2"]["rx_bytes"] == 123
    assert rows["ether2"]["up"] == 1
    assert rows["ether2"]["rx_drops"] == 1
    assert rows["ether2"]["rx_errors"] == 2
    assert rows["ether4"]["tx_packets"] == 4
    assert rows["ether4"]["up"] == 0


def test_parse_routeros_bgp_sessions() -> None:
    rows = {row["neighbor"]: row for row in routeros.parse_bgp_sessions(_ROS_BGP)}
    assert rows["193.10.11.2"]["up"] == 1
    assert rows["10.0.0.9"]["up"] == 0


def test_srl_collect_interfaces_and_bgp() -> None:
    stats, oper = srl.collect_interfaces(
        [
            {
                "name": "ethernet-1/1",
                "oper-state": "up",
                "statistics": {
                    "in-octets": 10,
                    "in-error-packets": 1,
                    "in-discarded-packets": 2,
                },
            },
            {"name": "mgmt0", "oper-state": "up", "statistics": {"in-octets": 9}},
        ]
    )
    assert stats["ethernet-1/1"]["in-error-packets"] == 1
    assert oper["ethernet-1/1"] == "up"
    peers = srl.collect_bgp_peers(
        {"peer-address": "10.0.0.2", "session-state": "established"}
    )
    assert peers == [{"neighbor": "10.0.0.2", "state": "established"}]


def test_linux_to_ether() -> None:
    assert routeros.linux_to_ether("eth0") == "ether1"
    assert routeros.linux_to_ether("eth3") == "ether4"


def test_routeros_setup_script_assigns_extra_ether_and_traffic_flow() -> None:
    script = routeros.setup_script(
        {"mgmt_iface": "eth3", "prefixlen": 16},
        "172.28.0.1",
        collector="172.28.255.254",
    )
    assert "nika" not in script.lower()
    assert "ether4" in script
    assert "172.28.0.1/16" in script
    assert "traffic-flow" in script
    assert "172.28.255.254" in script
    assert "9995" in script


def test_prometheus_collector_adds_routeros_job() -> None:
    files = prometheus.collector_files(
        {"router1": "172.28.0.1", "pc1": "172.28.0.2"},
        versions={"router1": "ros-7.21.5", "pc1": "1.5.0"},
    )
    config = json.loads(files[prometheus.PROMETHEUS_CONFIG])
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    assert jobs["node"]["static_configs"][0]["labels"]["node"] == "pc1"
    assert jobs["routeros"]["static_configs"][0]["targets"] == [
        f"127.0.0.1:{routeros.EXPORTER_PORT}"
    ]
    assert routeros.EXPORTER in files
    lines = prometheus.collector_lines(
        {"router1": "172.28.0.1", "pc1": "172.28.0.2"},
        versions={"router1": "ros-7.21.5", "pc1": "1.5.0"},
        hidden="ether4",
    )
    joined = "\n".join(lines)
    assert routeros.EXPORTER in joined
    assert "ether4" in joined
    assert "prometheus --config.file" in joined or "--config.file=" in joined
