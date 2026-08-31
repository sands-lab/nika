"""Unit tests for Kubernetes MCP server selection and session-scoped client."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nika.mcp.k8s.client import (
    K8sClient,
    get_client,
    reset_client,
    resolve_kubeconfig_path,
)
from nika.run_config.loader import reset_run_config, set_run_config
from nika.run_config.schema import RunConfig
from nika.mcp.registry import (
    K8S_MCP_SERVER,
    select_diagnosis_servers,
)


class TestK8sMcpSelection:
    def test_k8s_lab_includes_k8s_mcp_and_frr(self) -> None:
        servers = select_diagnosis_servers("k8s_lab", backend="kathara")
        assert K8S_MCP_SERVER in servers
        assert "kathara_frr_mcp_server" in servers
        assert "kathara_base_mcp_server" in servers

    def test_llmd_lab_includes_k8s_mcp(self) -> None:
        servers = select_diagnosis_servers("llmd_lab", backend="kathara")
        assert K8S_MCP_SERVER in servers

    def test_simple_bgp_excludes_k8s_mcp(self) -> None:
        servers = select_diagnosis_servers("simple_bgp", backend="kathara")
        assert K8S_MCP_SERVER not in servers

    def test_kubectl_only_skips_k8s_mcp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NIKA_K8S_ACCESS", raising=False)
        reset_run_config()
        set_run_config(
            RunConfig.model_validate({"nika": {"k8s": {"access": "kubectl_only"}}})
        )
        try:
            servers = select_diagnosis_servers("k8s_lab", backend="kathara")
            assert K8S_MCP_SERVER not in servers
        finally:
            reset_run_config()


class TestResolveKubeconfig:
    def test_prefers_metadata_path(self, tmp_path: Path) -> None:
        kube = tmp_path / "kubeconfig.yaml"
        kube.write_text("apiVersion: v1\n", encoding="utf-8")
        meta = {
            "session_id": "s1",
            "metadata": {"kubeconfig_path": str(kube)},
        }
        assert resolve_kubeconfig_path(meta) == kube

    def test_falls_back_to_runtime_workdir(self, tmp_path: Path) -> None:
        kube = tmp_path / "kubeconfig.yaml"
        kube.write_text("apiVersion: v1\n", encoding="utf-8")
        meta = {
            "session_id": "s1",
            "runtime_workdir": str(tmp_path),
            "metadata": {},
        }
        assert resolve_kubeconfig_path(meta) == kube


class TestSessionScopedClient:
    def test_get_client_caches_per_session(self, tmp_path: Path) -> None:
        reset_client()
        kube_a = tmp_path / "a.yaml"
        kube_b = tmp_path / "b.yaml"
        kube_a.write_text("apiVersion: v1\n", encoding="utf-8")
        kube_b.write_text("apiVersion: v1\n", encoding="utf-8")

        def meta_for(session_id: str, kube: Path) -> dict:
            return {
                "session_id": session_id,
                "status": "running",
                "metadata": {"kubeconfig_path": str(kube)},
            }

        with (
            patch(
                "nika.mcp.k8s.client.require_session_id",
                side_effect=["sess-a", "sess-a", "sess-b"],
            ),
            patch(
                "nika.mcp.k8s.client.get_session_meta",
                side_effect=[
                    meta_for("sess-a", kube_a),
                    meta_for("sess-b", kube_b),
                ],
            ),
        ):
            c1 = get_client()
            c2 = get_client()
            c3 = get_client()
        assert c1 is c2
        assert c1 is not c3
        assert c1.kubeconfig == str(kube_a)
        assert c3.kubeconfig == str(kube_b)
        reset_client()


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
