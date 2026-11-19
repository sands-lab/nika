from types import SimpleNamespace

import pytest

from nika.problems.support.dhcp import client_subnet
from nika.problems.support.oracle import service_address, service_addresses
from nika.problems.traffic_queueing_resource.tcp_rwnd_helpers import primary_ipv4
from nika.validation.symptom.addressing_probes import lab_addresses
from nika.validation.symptom.routing_probes import host_addresses

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("reverse", [False, True])
def test_service_discovery_ignores_telemetry_in_either_order(reverse):
    lines = [
        "2: eth0 inet 10.2.0.4/24 scope global eth0",
        "3: eth1 inet 172.28.0.9/16 scope global eth1",
    ]
    output = "\n".join(reversed(lines) if reverse else lines)
    runtime = SimpleNamespace(
        backend="kathara",
        exec=lambda *args, **kwargs: output,
        list_nodes=lambda: ["host"],
    )
    assert service_addresses(output) == [("eth0", "10.2.0.4/24")]
    assert service_address(output, "eth0") == "10.2.0.4"
    assert primary_ipv4(runtime, "host") == "10.2.0.4"
    assert lab_addresses(runtime) == {"host": [("eth0", "10.2.0.4/24")]}
    assert host_addresses(runtime, "host") == {"10.2.0.4"}


def test_dhcp_subnet_does_not_use_telemetry_fallback():
    runtime = SimpleNamespace(
        get_host_ip=lambda *args, **kwargs: "172.28.0.1/16",
        exec=lambda node, command: (
            "2: eth0 inet 10.2.0.4/24 scope global eth0"
            if "dev eth0" in command
            else ""
        ),
    )
    assert client_subnet(runtime, "host") == "10.2.0.0"


@pytest.mark.parametrize("reverse", [False, True])
def test_runtime_address_lookup_ignores_telemetry(reverse):
    import json

    from nika.service.lab.semantic_mixin import _ipv4_by_iface

    links = [
        {
            "ifname": "eth0",
            "addr_info": [{"family": "inet", "local": "10.2.0.4", "prefixlen": 24}],
        },
        {
            "ifname": "eth1",
            "addr_info": [{"family": "inet", "local": "172.28.0.9", "prefixlen": 16}],
        },
    ]
    assert _ipv4_by_iface(
        json.dumps(list(reversed(links)) if reverse else links),
        node="host",
        with_prefix=True,
    ) == {"eth0": "10.2.0.4/24"}


@pytest.mark.parametrize("reverse", [False, True])
def test_pingmesh_selects_service_address_on_declared_interface(reverse):
    import json

    from nika.telemetry.pingmesh import endpoint_address

    addresses = [
        {"scope": "global", "local": "172.28.0.9"},
        {"scope": "global", "local": "10.2.0.4"},
    ]
    rows = [
        {
            "ifname": "eth0",
            "addr_info": list(reversed(addresses)) if reverse else addresses,
        }
    ]
    runtime = SimpleNamespace(exec=lambda *args, **kwargs: json.dumps(rows))
    assert endpoint_address(runtime, "host", ["eth0"]) == "10.2.0.4"


@pytest.mark.parametrize("reverse", [False, True])
def test_containerlab_management_interface_is_not_a_probe_peer(tmp_path, reverse):
    topology = tmp_path / "lab.yaml"
    topology.write_text(
        "name: lab\ntopology:\n  nodes: {}\n  links:\n"
        "    - endpoints: ['host:eth1', 'router:eth1']\n"
    )
    lines = [
        "2: eth0 inet 172.20.20.5/24 scope global eth0",
        "3: eth1 inet 10.2.0.4/24 scope global eth1",
    ]
    output = "\n".join(reversed(lines) if reverse else lines)
    runtime = SimpleNamespace(
        backend="containerlab",
        topology_file=topology,
        exec=lambda *args, **kwargs: output,
        list_nodes=lambda: ["host"],
    )
    assert primary_ipv4(runtime, "host") == "10.2.0.4"
    assert lab_addresses(runtime) == {"host": [("eth1", "10.2.0.4/24")]}
    assert host_addresses(runtime, "host") == {"10.2.0.4"}


def test_runtime_lookup_uses_containerlab_topology(tmp_path):
    import json

    from nika.service.lab.semantic_mixin import SemanticOpsMixin

    topology = tmp_path / "lab.yaml"
    topology.write_text(
        "name: lab\ntopology:\n  nodes: {}\n  links:\n"
        "    - endpoints: ['host:eth1', 'router:eth1']\n"
        "    - endpoints: ['other:eth0', 'router:eth2']\n"
    )
    rows = [
        {
            "ifname": "eth0",
            "addr_info": [{"family": "inet", "local": "172.20.20.5", "prefixlen": 24}],
        },
        {
            "ifname": "eth1",
            "addr_info": [{"family": "inet", "local": "10.2.0.4", "prefixlen": 24}],
        },
    ]

    class API(SemanticOpsMixin):
        _runtime = SimpleNamespace(backend="containerlab", topology_file=topology)

        def exec_cmd(self, *args):
            return json.dumps(rows)

    assert API().get_host_ip("host") == "10.2.0.4"
    assert API().get_host_ip("other") == "172.20.20.5"
    rows[1]["addr_info"][0]["local"] = "172.20.21.5"
    assert API().get_host_ip("host") == "172.20.21.5"
    assert API().get_data_plane_host_ip("host") == "172.20.21.5"


def test_runtime_fallback_rejects_docker_bridge_management(monkeypatch):
    import ipaddress
    import json

    from nika.service.lab import semantic_mixin

    monkeypatch.setattr(
        semantic_mixin,
        "_docker_bridge_networks",
        lambda: (ipaddress.IPv4Network("172.17.0.0/16"),),
    )

    class API(semantic_mixin.SemanticOpsMixin):
        def exec_cmd(self, *args):
            return json.dumps(
                [
                    {
                        "ifname": "eth1",
                        "addr_info": [{"family": "inet", "local": "172.17.0.5"}],
                    }
                ]
            )

    assert API().get_host_ip("host") is None
    assert API().get_data_plane_host_ip("host") is None


@pytest.mark.parametrize("reverse", [False, True])
def test_discovery_excludes_docker_bridge(monkeypatch, reverse):
    import ipaddress

    monkeypatch.setattr(
        "nika.service.lab.semantic_mixin._docker_bridge_networks",
        lambda: (ipaddress.IPv4Network("172.17.0.0/16"),),
    )
    lines = [
        "2: eth0 inet 10.2.0.4/24 scope global eth0",
        "3: eth1 inet 172.17.0.9/16 scope global eth1",
    ]
    output = "\n".join(reversed(lines) if reverse else lines)
    runtime = SimpleNamespace(
        backend="kathara",
        exec=lambda *args, **kwargs: output,
        list_nodes=lambda: ["host"],
    )
    assert primary_ipv4(runtime, "host") == "10.2.0.4"
    assert lab_addresses(runtime) == {"host": [("eth0", "10.2.0.4/24")]}
    assert host_addresses(runtime, "host") == {"10.2.0.4"}
    runtime.exec = lambda *args, **kwargs: lines[1]
    assert primary_ipv4(runtime, "host") is None


def test_primary_ipv4_accepts_bridge_data_interface():
    runtime = SimpleNamespace(
        backend="kathara",
        exec=lambda *args, **kwargs: "2: br0 inet 10.2.0.4/24 scope global br0",
    )
    assert primary_ipv4(runtime, "host") == "10.2.0.4"


def test_kathara_retains_service_in_containerlab_management_range(monkeypatch):
    import ipaddress
    import json

    from nika.service.lab import semantic_mixin

    monkeypatch.setattr(
        semantic_mixin,
        "_docker_bridge_networks",
        lambda: (ipaddress.IPv4Network("172.17.0.0/16"),),
    )

    class API(semantic_mixin.SemanticOpsMixin):
        _runtime = SimpleNamespace(backend="kathara")

        def exec_cmd(self, *args):
            return json.dumps(
                [
                    {
                        "ifname": "eth1",
                        "addr_info": [{"family": "inet", "local": "172.20.21.5"}],
                    }
                ]
            )

    assert API().get_host_ip("host") == "172.20.21.5"
    assert API().get_data_plane_host_ip("host") == "172.20.21.5"
