"""Management-network attach stays off the agent topology."""

from __future__ import annotations

import pytest

from nika.net_env.net_env_pool import get_net_env_instance
from nika.telemetry import TELEMETRY_SUBNET, containerlab, management

pytestmark = pytest.mark.unit

_MIN3CLOS = {
    "name": "min3clos",
    "mgmt": {
        "ipv4-subnet": "172.100.1.0/24",
        "ipv6-subnet": "3fff:172:100:1::0/64",
    },
    "topology": {
        "nodes": {
            "leaf1": {
                "kind": "nokia_srlinux",
                "mgmt-ipv4": "172.100.1.2",
                "mgmt-ipv6": "3fff:172:100:1::2",
            },
            "leaf2": {
                "kind": "nokia_srlinux",
                "mgmt-ipv4": "172.100.1.3",
                "mgmt-ipv6": "3fff:172:100:1::3",
            },
            "spine": {
                "kind": "nokia_srlinux",
                "mgmt-ipv4": "172.100.1.4",
                "mgmt-ipv6": "3fff:172:100:1::4",
            },
            "client1": {
                "kind": "linux",
                "mgmt-ipv4": "172.100.1.5",
                "mgmt-ipv6": "3fff:172:100:1::5",
            },
            "client2": {
                "kind": "linux",
                "mgmt-ipv4": "172.100.1.6",
                "mgmt-ipv6": "3fff:172:100:1::6",
            },
        },
        "links": [
            {"endpoints": ["leaf1:e1-1", "spine:e1-1"]},
            {"endpoints": ["leaf2:e1-1", "spine:e1-2"]},
            {"endpoints": ["client1:eth1", "leaf1:e1-2"]},
            {"endpoints": ["client2:eth1", "leaf2:e1-2"]},
        ],
    },
}


@pytest.mark.parametrize(
    "scenario",
    ("sdn_l3_clos", "p4_dc_fabric", "p4_dc_gateway"),
)
def test_management_nodes_stay_off_the_topology(scenario: str) -> None:
    net_env = get_net_env_instance(scenario, backend="kathara", topo_size="s")
    management.attach(net_env, ("netflow", "prometheus"))
    topology = str(net_env.get_topology())
    assert management.TELEMETRY_COLLECTOR not in topology
    assert management.TELEMETRY_SWITCH not in topology
    assert "nika_telemetry_" not in topology


def test_int_sink_is_not_a_telemetry_exporter() -> None:
    net_env = get_net_env_instance("p4_dc_gateway", backend="kathara", topo_size="s")
    plan = management.attach(net_env, ("prometheus", "syslog"))
    assert "collector" not in plan["devices"]
    assert "fabric_mgr" in plan["devices"]
    assert "gateway_1" in plan["devices"]


def test_min3clos_template_topology_hides_collector() -> None:
    net_env = get_net_env_instance("min3clos", backend="containerlab")
    topology = str(net_env.get_topology())
    assert management.TELEMETRY_COLLECTOR not in topology
    assert "e1-3" not in topology


def test_inject_collector_adds_sflow_links_and_mgmt_node() -> None:
    doc = {
        "name": _MIN3CLOS["name"],
        "mgmt": dict(_MIN3CLOS["mgmt"]),
        "topology": {
            "nodes": {
                name: dict(cfg) for name, cfg in _MIN3CLOS["topology"]["nodes"].items()
            },
            "links": [dict(link) for link in _MIN3CLOS["topology"]["links"]],
        },
    }
    plan = containerlab.inject_collector(
        doc, ("netflow", "prometheus"), gnmic="/tmp/gnmic"
    )
    nodes = doc["topology"]["nodes"]
    links = doc["topology"]["links"]
    collector = nodes[management.TELEMETRY_COLLECTOR]
    assert collector["mgmt-ipv4"] == "172.100.1.7"
    assert collector["mgmt-ipv6"] == "3fff:172:100:1::7"
    assert collector["binds"] == ["/tmp/gnmic:/usr/local/bin/gnmic:ro"]
    assert plan["subnet"] == TELEMETRY_SUBNET
    assert plan["collector_flow_ip"].startswith("172.28.")
    assert plan["devices"]["leaf1"]["srl_e1"] == 3
    assert plan["devices"]["leaf1"]["data_e1"] == [1, 2]
    assert {"leaf1:e1-3", f"{management.TELEMETRY_COLLECTOR}:eth1"} <= {
        endpoint for link in links for endpoint in link["endpoints"]
    }
    after = get_net_env_instance("min3clos", backend="containerlab")
    assert management.TELEMETRY_COLLECTOR not in str(after.get_topology())


def test_multi_router_clab_injects_sflow_on_every_srl_node() -> None:
    doc = {
        "name": "isp_pdh",
        "mgmt": {
            "ipv4-subnet": "172.100.2.0/24",
            "ipv6-subnet": "3fff:172:100:2::0/64",
        },
        "topology": {
            "nodes": {
                "a": {
                    "kind": "nokia_srlinux",
                    "mgmt-ipv4": "172.100.2.2",
                    "mgmt-ipv6": "3fff:172:100:2::2",
                },
                "b": {
                    "kind": "nokia_srlinux",
                    "mgmt-ipv4": "172.100.2.3",
                    "mgmt-ipv6": "3fff:172:100:2::3",
                },
                "c": {
                    "kind": "nokia_srlinux",
                    "mgmt-ipv4": "172.100.2.4",
                    "mgmt-ipv6": "3fff:172:100:2::4",
                },
                "h_a": {"kind": "linux", "mgmt-ipv4": "172.100.2.5"},
                "h_b": {"kind": "linux", "mgmt-ipv4": "172.100.2.6"},
            },
            "links": [
                {"endpoints": ["a:e1-1", "b:e1-1"]},
                {"endpoints": ["b:e1-2", "c:e1-1"]},
                {"endpoints": ["h_a:eth1", "a:e1-2"]},
                {"endpoints": ["h_b:eth1", "b:e1-3"]},
            ],
        },
    }
    plan = containerlab.inject_collector(
        doc, ("netflow", "prometheus"), gnmic="/usr/bin/gnmic"
    )
    nodes = doc["topology"]["nodes"]
    assert management.TELEMETRY_COLLECTOR in nodes
    assert plan["devices"]["a"]["srl_e1"] == 3
    assert plan["devices"]["b"]["srl_e1"] == 4
    assert plan["devices"]["c"]["srl_e1"] == 2
    assert plan["devices"]["h_a"]["srl_e1"] is None
    endpoints = {
        endpoint for link in doc["topology"]["links"] for endpoint in link["endpoints"]
    }
    assert "a:e1-3" in endpoints
    assert "c:e1-2" in endpoints


def test_iosxr_attach_keeps_xr_interfaces_off_the_mgmt_nic() -> None:
    net_env = get_net_env_instance("iosxr_simple_bgp")
    before = net_env.lab.machines["router1"].get_envs()["XR_INTERFACES"]
    management.attach(net_env, ("prometheus",))
    after = net_env.lab.machines["router1"].get_envs()["XR_INTERFACES"]
    assert after == before
    assert "linux:eth2," not in after + ","
    topology = str(net_env.get_topology())
    assert management.TELEMETRY_COLLECTOR not in topology
    assert "nika_telemetry_" not in topology
    assert 2 in net_env.lab.machines["router1"].interfaces


def test_routeros_attach_does_not_raise_clab_intfs() -> None:
    net_env = get_net_env_instance("routeros_simple_bgp")
    before = net_env.lab.machines["router1"].get_envs()["CLAB_INTFS"]
    management.attach(net_env, ("netflow", "prometheus"))
    assert net_env.lab.machines["router1"].get_envs()["CLAB_INTFS"] == before
    topology = str(net_env.get_topology())
    assert management.TELEMETRY_COLLECTOR not in topology
    assert 3 in net_env.lab.machines["router1"].interfaces


def test_inject_collector_syslog_only_skips_sflow_links() -> None:
    doc = {
        "name": _MIN3CLOS["name"],
        "mgmt": dict(_MIN3CLOS["mgmt"]),
        "topology": {
            "nodes": {
                name: dict(cfg) for name, cfg in _MIN3CLOS["topology"]["nodes"].items()
            },
            "links": [dict(link) for link in _MIN3CLOS["topology"]["links"]],
        },
    }
    plan = containerlab.inject_collector(doc, ("syslog",), gnmic="/usr/bin/gnmic")
    assert plan["collector_flow_ip"] == ""
    assert "syslog" in plan["kinds"]
    assert all(
        management.TELEMETRY_COLLECTOR not in endpoint
        for link in doc["topology"]["links"]
        for endpoint in link["endpoints"]
    )


def test_inject_collector_prometheus_only_skips_sflow_links() -> None:
    doc = {
        "name": _MIN3CLOS["name"],
        "mgmt": dict(_MIN3CLOS["mgmt"]),
        "topology": {
            "nodes": {
                name: dict(cfg) for name, cfg in _MIN3CLOS["topology"]["nodes"].items()
            },
            "links": [dict(link) for link in _MIN3CLOS["topology"]["links"]],
        },
    }
    plan = containerlab.inject_collector(doc, ("prometheus",), gnmic="/usr/bin/gnmic")
    assert plan["collector_flow_ip"] == ""
    assert plan["devices"]["leaf1"]["srl_e1"] is None
    assert all(
        management.TELEMETRY_COLLECTOR not in endpoint
        for link in doc["topology"]["links"]
        for endpoint in link["endpoints"]
    )


def test_inject_collector_runs_sonic_flow_probes_in_router_netns() -> None:
    doc = {
        "name": "min3clos",
        "mgmt": {"ipv4-subnet": "172.100.1.0/24"},
        "topology": {
            "nodes": {
                "leaf1": {"kind": "sonic-vs", "mgmt-ipv4": "172.100.1.2"},
                "spine": {"kind": "sonic-vs", "mgmt-ipv4": "172.100.1.4"},
                "client1": {"kind": "linux", "mgmt-ipv4": "172.100.1.5"},
            },
            "links": [
                {"endpoints": ["leaf1:eth1", "spine:eth1"]},
                {"endpoints": ["client1:eth1", "leaf1:eth2"]},
            ],
        },
    }
    plan = containerlab.inject_collector(
        doc, ("netflow", "prometheus", "syslog"), gnmic=None
    )
    nodes = doc["topology"]["nodes"]
    probe = plan["devices"]["leaf1"]["flow_probe"]
    assert nodes[probe]["network-mode"] == "container:leaf1"
    assert "mgmt-ipv4" not in nodes[probe]
    assert probe not in plan["devices"]
    assert plan["devices"]["leaf1"]["data_ports"] == ["Ethernet0", "Ethernet4"]
    assert plan["devices"]["spine"]["data_ports"] == ["Ethernet0"]
    assert "flow_probe" not in plan["devices"]["client1"]
    # SONiC exports over the management network, not over extra links.
    assert plan["collector_ifaces"] == []
    assert len(doc["topology"]["links"]) == 2
