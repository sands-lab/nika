"""Unit tests for Kubernetes MCP server selection and helpers."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from nika.service.k8s_mcp_server.client import K8sClient
from nika.service.mcp_gateway.k8s_upstream import container_ipv4, k8s_mcp_upstream_url
from nika.service.mcp_gateway.remote_proxy import _upstream_url
from nika.service.mcp_server.registry import (
    K8S_MCP_SERVER,
    select_diagnosis_servers,
)


class TestK8sMcpSelection:
    def test_k8s_lab_includes_k8s_mcp_and_frr(self) -> None:
        servers = select_diagnosis_servers("k8s_lab", backend="kathara")
        assert K8S_MCP_SERVER in servers
        assert "kathara_frr_mcp_server" in servers
        assert "kathara_base_mcp_server" in servers

    def test_simple_bgp_excludes_k8s_mcp(self) -> None:
        servers = select_diagnosis_servers("simple_bgp", backend="kathara")
        assert K8S_MCP_SERVER not in servers


class TestUpstreamHelpers:
    def test_container_ipv4_from_networks(self) -> None:
        container = SimpleNamespace(
            attrs={
                "NetworkSettings": {
                    "Networks": {"bridge": {"IPAddress": "172.17.0.5"}},
                    "IPAddress": "",
                }
            },
            name="controller",
        )
        assert container_ipv4(container) == "172.17.0.5"

    def test_upstream_url_preserves_mcp_path(self) -> None:
        assert (
            _upstream_url("http://172.17.0.5:18765", "/mcp", "")
            == "http://172.17.0.5:18765/mcp"
        )
        assert (
            _upstream_url("http://172.17.0.5:18765", "/mcp", "sessionId=1")
            == "http://172.17.0.5:18765/mcp?sessionId=1"
        )

    def test_k8s_mcp_upstream_url(self) -> None:
        container = SimpleNamespace(
            attrs={
                "NetworkSettings": {
                    "Networks": {"kathara": {"IPAddress": "10.10.0.2"}},
                }
            },
            name="c",
        )
        with patch(
            "nika.service.mcp_gateway.k8s_upstream.get_machine_container",
            return_value=container,
        ):
            assert (
                k8s_mcp_upstream_url(lab_name="k8s_lab__x") == "http://10.10.0.2:18765"
            )


class TestK8sClientHelpers:
    def test_list_nodes_shapes_ready_flag(self) -> None:
        k8s = K8sClient()
        k8s._loaded = True
        node = MagicMock()
        node.metadata.name = "worker1"
        node.spec.unschedulable = False
        cond = MagicMock()
        cond.type = "Ready"
        cond.status = "True"
        node.status.conditions = [cond]
        addr = MagicMock()
        addr.type = "InternalIP"
        addr.address = "201.2.1.2"
        node.status.addresses = [addr]
        core = MagicMock()
        core.list_node.return_value = SimpleNamespace(items=[node])
        k8s._core = core

        rows = k8s.list_nodes()
        assert rows == [
            {
                "name": "worker1",
                "ready": True,
                "schedulable": True,
                "internal_ip": "201.2.1.2",
                "conditions": {"Ready": "True"},
            }
        ]

    def test_get_endpoints_skips_unready(self) -> None:
        k8s = K8sClient()
        k8s._loaded = True
        k8s._api_client = MagicMock()

        ready_ep = MagicMock()
        ready_ep.conditions.ready = True
        ready_ep.addresses = ["10.42.0.1"]
        unready_ep = MagicMock()
        unready_ep.conditions.ready = False
        unready_ep.addresses = ["10.42.0.2"]

        slice_item = MagicMock()
        slice_item.endpoints = [ready_ep, unready_ep]
        slice_item.ports = []

        discovery = MagicMock()
        discovery.list_namespaced_endpoint_slice.return_value = SimpleNamespace(
            items=[slice_item]
        )
        k8s._discovery = discovery

        svc = MagicMock()
        svc.spec.cluster_ip = "10.43.0.10"
        core = MagicMock()
        core.read_namespaced_service.return_value = svc
        k8s._core = core

        result = k8s.get_endpoints("kube-dns", namespace="kube-system")
        assert result["addresses"] == ["10.42.0.1"]
        assert result["cluster_ip"] == "10.43.0.10"
        assert json.loads(json.dumps(result))["service"] == "kube-dns"
