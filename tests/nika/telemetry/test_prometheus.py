"""Prometheus kind: node_exporter, OpenFlow port stats, P4 counters."""

from __future__ import annotations

import json

import pytest

from nika.telemetry import prometheus

pytestmark = pytest.mark.unit

_DUMP_PORTS = """\
OFPST_PORT reply (OF1.3) (xid=0x2): 2 ports
  port  1: rx pkts=10, bytes=800, drop=1, errs=0, frame=0, over=0, crc=0
           tx pkts=5, bytes=400, drop=0, errs=2, coll=0
           duration=12.345s
  port LOCAL: rx pkts=0, bytes=0, drop=0, errs=0, frame=0, over=0, crc=0
           tx pkts=1, bytes=64, drop=0, errs=0, coll=0
           duration=12.345s
"""
_DUMP_DESC = """\
OFPST_PORT_DESC reply (OF1.3) (xid=0x2):
 1(eth0): addr:02:00:01:00:00:01
     config:     0
     state:      LIVE
 LOCAL(leaf_1): addr:02:00:01:00:00:00
     config:     0
     state:      LIVE
"""


class _Runtime:
    def __init__(self, output: str) -> None:
        self.output = output

    def exec(self, name: str, command: str, timeout: float = 10) -> str:
        return self.output


class _CmdRuntime:
    def __init__(self, outputs: dict[str, str]) -> None:
        self.outputs = outputs

    def exec(self, name: str, command: str, timeout: float = 10) -> str:
        return "\n".join(
            output for needle, output in self.outputs.items() if needle in command
        )


def test_parse_openflow_ports_maps_names() -> None:
    rows = prometheus.parse_openflow_ports(_DUMP_PORTS, _DUMP_DESC)
    assert rows[0]["device"] == "eth0"
    assert rows[0]["port"] == "1"
    assert rows[0]["rx_packets"] == 10
    assert rows[0]["tx_bytes"] == 400
    assert rows[1]["device"] == "leaf_1"
    assert rows[1]["port"] == "LOCAL"


def test_render_openflow_metrics_uses_port_labels() -> None:
    text = prometheus.render_openflow_metrics(
        prometheus.parse_openflow_ports(_DUMP_PORTS, _DUMP_DESC)
    )
    assert 'openflow_port_rx_packets{device="eth0",port="1"} 10' in text
    assert 'openflow_port_tx_errors{device="eth0",port="1"} 2' in text


def test_render_p4_metrics_labels_switches() -> None:
    text = prometheus.render_p4_metrics(
        {
            "ok": True,
            "counters": {
                "leaf_1": {
                    "ingress": {"1": {"packets": 9, "bytes": 90}},
                    "egress": {"2": {"packets": 4, "bytes": 40}},
                }
            },
        }
    )
    assert 'p4_ingress_packets{switch="leaf_1",port="1"} 9' in text
    assert 'p4_egress_bytes{switch="leaf_1",port="2"} 40' in text
    assert "internal_fault" not in text


def test_probe_node_exporter_appends_p4() -> None:
    runtime = _Runtime("node_exporter, version 1.5.0\n/usr/bin/socat\np4rt\n")
    assert prometheus.probe(runtime, "fabric_mgr") == "1.5.0+p4"


def test_probe_falls_back_to_srl() -> None:
    runtime = _Runtime("/usr/bin/sr_cli\nSoftware Version: v24.10.1\n")
    assert prometheus.probe(runtime, "leaf1").startswith("srl-")
    assert (
        prometheus.device_files(
            {}, "172.26.0.1", "172.26.255.254", version="srl-24.10.1"
        )
        == {}
    )
    assert prometheus.device_lines({}, "172.26.0.1", version="srl-24.10.1") == []


def test_probe_falls_back_to_iosxr() -> None:
    runtime = _CmdRuntime(
        {
            "test -x /pkg/bin/xr_cli": "xr_cli",
            "command -v python3": "/usr/bin/python3",
            "show version": "Cisco IOS XR Software, Version 26.2.1",
        }
    )
    assert prometheus.probe(runtime, "router1") == "xr-26.2.1"
    files = prometheus.device_files(
        {"mgmt_iface": "eth2"}, "172.28.0.1", "172.28.255.254", version="xr-26.2.1"
    )
    from nika.telemetry import iosxr

    assert iosxr.XR_EXPORTER in files
    assert "nika" not in files[iosxr.XR_EXPORTER].lower()
    lines = prometheus.device_lines(
        {"mgmt_iface": "eth2"}, "172.28.0.1", version="xr-26.2.1"
    )
    assert iosxr.XR_EXPORTER in lines[0]
    assert "prometheus-node-exporter" not in lines[0]


def test_probe_falls_back_to_routeros() -> None:
    runtime = _CmdRuntime(
        {
            "command -v sshpass": "/usr/bin/sshpass",
            "/system resource print": "version: 7.21.5",
        }
    )
    assert prometheus.probe(runtime, "router1") == "ros-7.21.5"
    assert (
        prometheus.device_files(
            {"mgmt_iface": "eth3"},
            "172.28.0.1",
            "172.28.255.254",
            version="ros-7.21.5",
        )
        == {}
    )
    assert (
        prometheus.device_lines(
            {"mgmt_iface": "eth3"}, "172.28.0.1", version="ros-7.21.5"
        )
        == []
    )


def test_probe_ovs_without_node_exporter() -> None:
    runtime = _Runtime(
        "ovs-vsctl (Open vSwitch) 3.1.0\n/usr/bin/python3\n/usr/bin/ovs-ofctl\n"
    )
    assert prometheus.probe(runtime, "leaf_1") == "ovs-3.1.0"


def test_ovs_exporter_binds_management_vrf() -> None:
    files = prometheus.device_files(
        {"mgmt_iface": "eth4", "data_ifaces": ["eth0"]},
        "172.28.0.3",
        "172.28.0.254",
        version="ovs-3.1.0",
    )
    script = files[prometheus.OVS_EXPORTER]
    assert "nika" not in script.lower()
    assert "SO_BINDTODEVICE" in script
    assert "openflow_port_" in script
    lines = prometheus.device_lines(
        {"mgmt_iface": "eth4"}, "172.28.0.3", version="ovs-3.1.0"
    )
    assert prometheus.OVS_EXPORTER in lines[0]
    assert "prometheus-node-exporter" not in lines[0]


def test_p4_textfile_runs_beside_node_exporter() -> None:
    files = prometheus.device_files(
        {"mgmt_iface": "eth1", "data_ifaces": ["eth0"]},
        "172.28.0.5",
        "172.28.0.254",
        version="1.5.0+p4",
    )
    assert prometheus.P4_EXPORTER in files
    assert "nika" not in files[prometheus.P4_EXPORTER].lower()
    lines = prometheus.device_lines(
        {"mgmt_iface": "eth1"}, "172.28.0.5", version="1.5.0+p4"
    )
    joined = "\n".join(lines)
    assert "prometheus-node-exporter" in joined
    assert "--collector.textfile" in joined
    assert prometheus.P4_EXPORTER in joined


def test_frr_bgp_textfile_runs_beside_node_exporter() -> None:
    files = prometheus.device_files(
        {"mgmt_iface": "eth2", "data_ifaces": ["eth0"]},
        "172.28.0.3",
        "172.28.0.254",
        version="1.5.0",
    )
    assert prometheus.FRR_BGP_EXPORTER in files
    assert "frr_bgp_peer_up" in files[prometheus.FRR_BGP_EXPORTER]
    lines = prometheus.device_lines(
        {"mgmt_iface": "eth2"}, "172.28.0.3", version="1.5.0"
    )
    assert prometheus.FRR_BGP_EXPORTER in "\n".join(lines)


def test_parse_frr_bgp_summary() -> None:
    rows = prometheus.parse_frr_bgp_summary(
        {
            "ipv4Unicast": {
                "peers": {
                    "193.10.11.2": {"state": "Established", "pfxRcd": 4},
                    "10.0.0.9": {"state": "Idle", "pfxRcd": 0},
                }
            }
        }
    )
    by_peer = {row["neighbor"]: row for row in rows}
    assert by_peer["193.10.11.2"] == {
        "neighbor": "193.10.11.2",
        "up": 1,
        "prefixes": 4,
    }
    assert by_peer["10.0.0.9"]["up"] == 0


def test_collector_relabels_p4_switch_onto_node() -> None:
    files = prometheus.collector_files({"fabric_mgr": "172.28.0.5"})
    config = json.loads(files[prometheus.PROMETHEUS_CONFIG])
    relabel = config["scrape_configs"][0]["metric_relabel_configs"]
    assert relabel[0]["target_label"] == "node"
    assert relabel[0]["regex"] == "p4_.+;(.+)"
