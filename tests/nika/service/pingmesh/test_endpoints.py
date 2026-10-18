from __future__ import annotations

import pytest
from collections import defaultdict
from unittest.mock import MagicMock
from nika.service.pingmesh.endpoints import (
    discover_endpoints,
    is_endpoint_node_name,
    is_excluded_node_name,
)

pytestmark = pytest.mark.unit


class EndpointNameHeuristicsTest:
    def test_endpoint_names(self) -> None:

        assert is_endpoint_node_name("client1")

        assert is_endpoint_node_name("pc1")

        assert is_endpoint_node_name("web_server_1")

    def test_excluded_names(self) -> None:

        assert is_excluded_node_name("leaf1")

        assert is_excluded_node_name("router1")

        assert not is_endpoint_node_name("leaf1")


class DiscoverEndpointsTest:
    def test_kathara_hosts_and_servers(self) -> None:
        api = MagicMock()
        api.hosts = ["pc1", "pc2"]
        api.servers = defaultdict(list)
        api.servers["web"] = ["web_server_1"]
        discovered = discover_endpoints(api)
        api.load_machines.assert_called_once()

        assert discovered == ["pc1", "pc2", "web_server_1"]

    def test_containerlab_filters_routers(self) -> None:
        api = MagicMock()
        api.runtime.list_nodes.return_value = ["client1", "client2", "leaf1", "spine"]
        from nika.service.containerlab.base_api import ContainerlabBaseAPI

        api.__class__ = ContainerlabBaseAPI
        discovered = discover_endpoints(api)

        assert discovered == ["client1", "client2"]

    def test_containerlab_includes_server(self) -> None:
        api = MagicMock()
        api.runtime.list_nodes.return_value = ["client1", "dns_server"]
        from nika.service.containerlab.base_api import ContainerlabBaseAPI

        api.__class__ = ContainerlabBaseAPI
        discovered = discover_endpoints(api)

        assert discovered == ["client1", "dns_server"]

    def test_containerlab_resolves_eth1_first(self) -> None:
        api = MagicMock()
        api.get_host_ip.side_effect = lambda name, iface="eth0": {
            ("client1", "eth1"): "10.0.0.25",
            ("client1", "eth0"): "172.100.100.5",
        }.get((name, iface))
        from nika.service.containerlab.base_api import ContainerlabBaseAPI
        from nika.service.pingmesh.endpoints import resolve_endpoint_ip

        api.__class__ = ContainerlabBaseAPI

        assert resolve_endpoint_ip(api, "client1") == "10.0.0.25"
