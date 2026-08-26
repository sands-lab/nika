from __future__ import annotations

from nika.problems.root_cause import (
    FaultResource,
    ResourceKind,
    RootCause,
    canonical_root_causes,
    healthy_ground_truth,
    interface_resource,
    k8s_resource,
    node_resource,
    resource_from_id,
)


class RootCauseSchemaTest:
    def test_interface_id(self) -> None:
        res = interface_resource("pc1", "eth0")
        assert res.kind == ResourceKind.INTERFACE
        assert res.id == "interface/pc1/eth0"

    def test_k8s_namespaced_id(self) -> None:
        res = k8s_resource("NetworkPolicy", "nika-deny-ingress", namespace="word-ns")
        assert res.id == "k8s/NetworkPolicy/word-ns/nika-deny-ingress"

    def test_resource_from_id(self) -> None:
        assert resource_from_id("node/pc1").id == "node/pc1"
        assert resource_from_id("interface/pc1/eth0").id == "interface/pc1/eth0"
        assert (
            resource_from_id("k8s/Service/kube-system/kube-dns").id
            == "k8s/Service/kube-system/kube-dns"
        )

    def test_submit_shape_resource_id(self) -> None:
        cause = RootCause(resource_id="interface/pc1/eth0", fault_type="link_down")
        assert cause.resource is not None
        assert cause.resource.id == "interface/pc1/eth0"
        assert cause.pair_key() == ("interface/pc1/eth0", "link_down")

    def test_canonical_sort(self) -> None:
        items = [
            RootCause(resource=node_resource("b"), fault_type="host_missing_ip"),
            RootCause(resource=node_resource("a"), fault_type="host_missing_ip"),
        ]
        dumped = canonical_root_causes(items)
        assert dumped[0]["resource"] == {"kind": "node", "node": "a"}
        assert dumped[1]["resource"] == {"kind": "node", "node": "b"}

    def test_healthy_empty(self) -> None:
        gt = healthy_ground_truth()
        assert gt.is_anomaly is False
        assert gt.root_causes == []
        assert gt.schema_version == 3

    def test_resource_roundtrip(self) -> None:
        original = interface_resource("leaf1", "e1-1")
        parsed = FaultResource.model_validate(original.model_dump())
        assert parsed.id == original.id
