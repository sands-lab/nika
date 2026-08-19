"""Kubernetes service-forwarding failure implementations."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from nika.problems.root_cause import (
    UnresolvedRootCauseError,
    node_resource,
)

from nika.problems.support.kubernetes.base import K8sParams, K8sProblemBase

from nika.problems.support.kubernetes.node_filter import NodeFilter, NodeFilterError

from nika.problems.problem_base import (
    FailureDomain,
)
from nika.utils.logger import system_logger

logger = system_logger

DEFAULT_PROBE_SERVICE = "kubernetes"

DEFAULT_PROBE_NAMESPACE = "default"


class ClusterIPRoutingBrokenParams(K8sParams):
    """Parameters for breaking ClusterIP Service routing on one node."""

    node_name: str = Field(
        default="",
        description=(
            "k3s node device whose Service dataplane is broken. "
            "Defaults to the first non-control-plane cluster node."
        ),
    )
    service_name: str = Field(
        default="",
        description=(
            "Restrict the fault to a single Service's ClusterIP. "
            "When omitted, the whole Service CIDR is blocked, so every "
            "ClusterIP Service (in-cluster DNS included) fails on that node."
        ),
    )
    namespace: str = Field(
        default="",
        description="Namespace of `service_name`; ignored in Service-CIDR mode.",
    )
    service_cidr: str = Field(
        default="",
        description="Override the discovered Service CIDR (e.g. 10.43.0.0/16).",
    )


def _as_port(value: Any) -> int | None:
    """Return ``value`` as a port number, or ``None`` for named target ports."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ClusterIPRoutingBroken(K8sProblemBase):
    failure_domain = FailureDomain.SERVICE_NETWORKING
    root_cause_name: str = "k8s_clusterip_routing_broken"
    symptom_desc = (
        "Pods scheduled on one Kubernetes node cannot reach any ClusterIP Service, "
        "including in-cluster DNS, while direct pod-IP traffic from the same node "
        "still works. Services, endpoints and pods all report healthy, and the node "
        "stays Ready."
    )
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s", "kube_proxy"]

    Params = ClusterIPRoutingBrokenParams

    def __init__(self, scenario_name: str | None = None, **kwargs: Any) -> None:
        super().__init__(scenario_name, **kwargs)
        self.target_device: str | None = None
        self.blocked_target: str | None = None

    def _target_device(self, params: ClusterIPRoutingBrokenParams) -> str | None:
        if params.node_name:
            self.target_device = params.node_name
            return params.node_name
        if self.target_device is None:
            control = self.control_node(params)
            candidates = [node for node in self.cluster_nodes() if node != control]
            if not candidates:
                # Single-node cluster: the control plane is the only option.
                candidates = self.cluster_nodes()
            if not candidates:
                raise ValueError(
                    f"{type(self).__name__} found no k3s nodes in "
                    f"{self.scenario_name!r}; cannot break Service routing."
                )
            self.target_device = min(candidates)
        return self.target_device

    def _block_target(self, params: ClusterIPRoutingBrokenParams, k8s: Any) -> str:
        control = self.control_node(params)
        if params.service_name:
            namespace = params.namespace or DEFAULT_PROBE_NAMESPACE
            cluster_ip = k8s.k8s_service_cluster_ip(
                control, params.service_name, namespace=namespace
            )
            if not cluster_ip:
                raise ValueError(
                    f"Service {namespace}/{params.service_name} has no ClusterIP "
                    "(headless or missing); nothing to block."
                )
            return cluster_ip
        return params.service_cidr or k8s.k8s_service_cidr(control)

    def _probe_plan(
        self, params: ClusterIPRoutingBrokenParams, k8s: Any
    ) -> dict[str, Any]:
        control = self.control_node(params)
        if params.service_name:
            service = params.service_name
            namespace = params.namespace or DEFAULT_PROBE_NAMESPACE
        else:
            service = DEFAULT_PROBE_SERVICE
            namespace = DEFAULT_PROBE_NAMESPACE

        plan: dict[str, Any] = {
            "probe_service": f"{namespace}/{service}",
            "cluster_ip": "",
            "service_port": None,
            "backend_address": "",
            "backend_port": None,
        }
        try:
            plan["cluster_ip"] = k8s.k8s_service_cluster_ip(
                control, service, namespace=namespace
            )
            ports = k8s.k8s_service_ports(control, service, namespace=namespace)
            addresses = k8s.k8s_service_endpoint_addresses(
                control, service, namespace=namespace
            )
        except Exception as exc:  # noqa: BLE001 - probing is best-effort
            plan["probe_error"] = str(exc)
            return plan

        if ports:
            plan["service_port"] = _as_port(ports[0].get("port"))
            plan["backend_port"] = _as_port(ports[0].get("target_port"))
        if addresses:
            plan["backend_address"] = addresses[0]
        return plan

    def root_cause_resources(self, params: ClusterIPRoutingBrokenParams):
        node = (params.node_name or "").strip()
        if not node:
            raise UnresolvedRootCauseError(
                "k8s_clusterip_routing_broken needs node_name for a unique resource."
            )
        return [node_resource(node)]

    def inject_fault(self, params: ClusterIPRoutingBrokenParams) -> None:
        k8s = self.runtime.lab_api
        device = self._target_device(params)
        target = self._block_target(params, k8s)
        self.blocked_target = target

        if params.service_name:
            namespace = params.namespace or DEFAULT_PROBE_NAMESPACE
            self.k8s_namespace = namespace
            self.record_k8s_object("Service", params.service_name, namespace=namespace)
        else:
            self.record_k8s_object("ServiceCIDR", target)

        node_filter = NodeFilter(self.runtime, device)
        node_filter.block_destination(target)

        k8s_node = k8s.k8s_device_for_node(
            self.control_node(params), device, devices=self.cluster_nodes()
        )
        logger.info(
            f"Blocked Service traffic to {target} on device {device} "
            f"(Kubernetes node {k8s_node}) using iptables: raw-table drops "
            "preempt kube-proxy DNAT, so ClusterIP Services fail while pod-IP "
            "traffic is unaffected."
        )

    def verify_fault(self, params: ClusterIPRoutingBrokenParams) -> dict:
        k8s = self.runtime.lab_api
        control = self.control_node(params)
        device = self._target_device(params)
        target = self.blocked_target or self._block_target(params, k8s)
        node_filter = NodeFilter(self.runtime, device)
        plan = self._probe_plan(params, k8s)

        def evaluate() -> tuple[bool, dict[str, Any]]:
            blocked = node_filter.blocked(target)
            rules_installed = bool(blocked.get("prerouting")) and bool(
                blocked.get("output")
            )

            cluster_ip = plan.get("cluster_ip") or ""
            service_port = plan.get("service_port")
            backend_address = plan.get("backend_address") or ""
            backend_port = plan.get("backend_port")

            clusterip_reachable: bool | None = None
            backend_reachable: bool | None = None
            if cluster_ip and service_port:
                clusterip_reachable = node_filter.tcp_reachable(
                    cluster_ip, service_port
                )
            if backend_address and backend_port:
                backend_reachable = node_filter.tcp_reachable(
                    backend_address, backend_port
                )

            # The Service object must still look healthy: a dataplane-only fault
            # is the whole diagnostic signature here.
            service_intact = True
            if params.service_name:
                service_intact = bool(
                    k8s.k8s_service_cluster_ip(
                        control,
                        params.service_name,
                        namespace=params.namespace or DEFAULT_PROBE_NAMESPACE,
                    )
                )

            details: dict[str, Any] = {
                "target_device": device,
                "blocked_target": target,
                "raw_prerouting_blocked": bool(blocked.get("prerouting")),
                "raw_output_blocked": bool(blocked.get("output")),
                "clusterip_reachable": clusterip_reachable,
                "backend_reachable": backend_reachable,
                "service_object_intact": service_intact,
                **plan,
            }
            if not rules_installed:
                details["ruleset"] = node_filter.rules_dump()[:1000]

            verified = rules_installed and service_intact
            return verified, details

        def check() -> tuple[bool, dict[str, Any]]:
            try:
                return evaluate()
            except NodeFilterError as exc:
                # A transient exec failure should retry inside the poll loop
                # rather than abort: poll_verify only absorbs K8sCommandError.
                return False, {
                    "target_device": device,
                    "blocked_target": target,
                    "error": str(exc),
                }

        return self.poll_verify(check)
