"""Kubernetes service-networking and naming faults."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from nika.problems.root_cause import (
    k8s_resource,
)
from nika.problems.support.kubernetes.base import K8sParams, K8sProblemBase
from nika.problems.support.kubernetes.node_filter import (
    DropSpec,
    NodeFilter,
    NodeFilterError,
)
from nika.problems.problem_base import (
    FailureCause,
    FailureDomain,
    FailureImpact,
    FailureScope,
    FailureSymptom,
    FailureTemporal,
)
from nika.utils.logger import system_logger

logger = system_logger

#: Probed when the fault blocks the whole Service CIDR: the ``kubernetes``
#: Service exists in every cluster and its endpoint is the apiserver, so the
#: "ClusterIP fails / direct address works" contrast can be checked in any lab.

#: CoreDNS in k3s: Deployment ``coredns`` behind Service ``kube-dns``.
DNS_SERVICE = "kube-dns"
DNS_NAMESPACE = "kube-system"
DNS_SELECTOR = "k8s-app=kube-dns"
DNS_PORT = 53
DNS_PROTOCOLS = ("udp", "tcp")
#: CoreDNS metrics port: left reachable on purpose, so verification can show
#: that the CoreDNS pod is up and routable while only DNS itself is cut.
DNS_METRICS_PORT = 9153


class CoreDNSIsolationParams(K8sParams):
    """Parameters for isolating CoreDNS from the rest of the cluster."""

    node_name: str = Field(
        default="",
        description=(
            "Device to filter DNS traffic on. Defaults to every node hosting a "
            "CoreDNS pod, which isolates CoreDNS cluster-wide; naming another "
            "node instead cuts DNS only for the pods scheduled there."
        ),
    )
    dns_service: str = Field(
        default=DNS_SERVICE, description="DNS Service name (k3s uses `kube-dns`)."
    )
    dns_namespace: str = Field(
        default=DNS_NAMESPACE, description="Namespace holding the DNS Service."
    )
    dns_selector: str = Field(
        default=DNS_SELECTOR, description="Label selector matching the CoreDNS pods."
    )
    dns_port: int = Field(default=DNS_PORT, description="DNS port to drop.")
    include_pod_ips: bool = Field(
        default=True,
        description=(
            "Also drop DNS traffic addressed to the CoreDNS pod IPs. Required for a "
            "cluster-wide outage: queries from other nodes are DNAT-ed to the pod IP "
            "before they arrive, so a VIP-only rule would not match them."
        ),
    )


class CoreDNSIsolation(K8sProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.BLACKHOLE
    scope = FailureScope.SERVICE
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.COMPLETE
    root_cause_name: str = "k8s_coredns_isolated"
    symptom_desc = (
        "Applications cannot resolve Kubernetes service names such as "
        "*.svc.cluster.local and report DNS timeouts, while communication by IP "
        "address keeps working. The CoreDNS pods are Running and Ready and the DNS "
        "Service still lists its endpoints."
    )
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s", "coredns"]

    Params = CoreDNSIsolationParams

    def __init__(self, scenario_name: str | None = None, **kwargs: Any) -> None:
        super().__init__(scenario_name, **kwargs)
        self.target_devices: list[str] = []
        self.blocked_specs: list[str] = []

    def _dns_pod_nodes(self, params: CoreDNSIsolationParams, k8s: Any) -> list[str]:
        control = self.control_node(params)
        pods = k8s.k8s_pods(
            control, namespace=params.dns_namespace, selector=params.dns_selector
        )
        cluster_nodes = self.cluster_nodes()
        devices: list[str] = []
        for pod in pods:
            k8s_node = pod.get("node") or ""
            if not k8s_node:
                continue
            device = k8s.k8s_device_for_node(control, k8s_node, devices=cluster_nodes)
            if device not in devices:
                devices.append(device)
        return devices

    def _target_devices(self, params: CoreDNSIsolationParams, k8s: Any) -> list[str]:
        if params.node_name:
            self.target_devices = [params.node_name]
            return self.target_devices
        if not self.target_devices:
            devices = self._dns_pod_nodes(params, k8s)
            if not devices:
                raise ValueError(
                    f"{type(self).__name__} found no running CoreDNS pods matching "
                    f"{params.dns_selector!r} in namespace {params.dns_namespace!r}; "
                    "cannot isolate DNS."
                )
            self.target_devices = sorted(devices)
        return self.target_devices

    def _dns_destinations(
        self, params: CoreDNSIsolationParams, k8s: Any
    ) -> dict[str, Any]:
        control = self.control_node(params)
        cluster_ip = k8s.k8s_service_cluster_ip(
            control, params.dns_service, namespace=params.dns_namespace
        )
        if not cluster_ip:
            raise ValueError(
                f"Service {params.dns_namespace}/{params.dns_service} has no "
                "ClusterIP; cannot isolate DNS."
            )
        pod_ips: list[str] = []
        if params.include_pod_ips:
            pod_ips = k8s.k8s_service_endpoint_addresses(
                control, params.dns_service, namespace=params.dns_namespace
            )
        return {"cluster_ip": cluster_ip, "pod_ips": pod_ips}

    @staticmethod
    def _drop_specs(
        params: CoreDNSIsolationParams, destinations: dict[str, Any]
    ) -> list[DropSpec]:
        addresses = [destinations["cluster_ip"], *destinations["pod_ips"]]
        return [
            DropSpec(address, protocol=protocol, port=params.dns_port)
            for address in addresses
            for protocol in DNS_PROTOCOLS
        ]

    def _probe_device(self) -> str:
        targets = set(self.target_devices)
        for node in sorted(self.cluster_nodes()):
            if node not in targets:
                return node
        return self.target_devices[0]

    def root_cause_resources(self, params: CoreDNSIsolationParams):
        ns = params.dns_namespace or DNS_NAMESPACE
        name = params.dns_service or DNS_SERVICE
        return [k8s_resource("Service", name, namespace=ns)]

    def inject_fault(self, params: CoreDNSIsolationParams) -> None:
        k8s = self.runtime.lab_api
        devices = self._target_devices(params, k8s)
        destinations = self._dns_destinations(params, k8s)
        specs = self._drop_specs(params, destinations)

        self.k8s_namespace = params.dns_namespace
        self.record_k8s_object(
            "Service", params.dns_service, namespace=params.dns_namespace
        )
        self.blocked_specs = [spec.describe() for spec in specs]

        for device in devices:
            node_filter = NodeFilter(self.runtime, device)
            for spec in specs:
                node_filter.block(spec)

        logger.info(
            f"Isolated CoreDNS on {', '.join(devices)} using iptables: "
            f"dropped {'/'.join(DNS_PROTOCOLS)} port {params.dns_port} to "
            f"{destinations['cluster_ip']} and pod IPs "
            f"{destinations['pod_ips'] or ['<none>']}. Name resolution fails while "
            "IP connectivity and the CoreDNS pods themselves stay healthy."
        )

    def verify_fault(self, params: CoreDNSIsolationParams) -> dict:
        k8s = self.runtime.lab_api
        control = self.control_node(params)
        devices = self._target_devices(params, k8s)

        def evaluate() -> tuple[bool, dict[str, Any]]:
            destinations = self._dns_destinations(params, k8s)
            specs = self._drop_specs(params, destinations)

            per_device: dict[str, Any] = {}
            rules_installed = True
            for device in devices:
                node_filter = NodeFilter(self.runtime, device)
                unfiltered = [
                    f"{spec.describe()}:{chain}"
                    for spec in specs
                    for chain, present in node_filter.blocked_spec(spec).items()
                    if not present
                ]
                per_device[device] = {
                    "unfiltered": unfiltered,
                }
                rules_installed = rules_installed and not unfiltered

            # Contrast from an unfiltered node: DNS must fail while the CoreDNS
            # pod stays reachable on its metrics port.
            probe_device = self._probe_device()
            probe = NodeFilter(self.runtime, probe_device)
            pod_ips = destinations["pod_ips"]
            dns_reachable = probe.tcp_reachable(
                destinations["cluster_ip"], params.dns_port
            )
            metrics_reachable = (
                probe.tcp_reachable(pod_ips[0], DNS_METRICS_PORT) if pod_ips else None
            )

            # CoreDNS must look healthy: the fault is isolation, not a crash.
            dns_pods = k8s.k8s_pods(
                control, namespace=params.dns_namespace, selector=params.dns_selector
            )
            pods_ready = bool(dns_pods) and all(pod["ready"] for pod in dns_pods)

            details: dict[str, Any] = {
                "target_devices": devices,
                "probe_device": probe_device,
                "dns_service": f"{params.dns_namespace}/{params.dns_service}",
                "dns_cluster_ip": destinations["cluster_ip"],
                "dns_pod_ips": pod_ips,
                "dns_port": params.dns_port,
                "blocked_specs": [spec.describe() for spec in specs],
                "devices": per_device,
                "dns_port_reachable": dns_reachable,
                "coredns_metrics_reachable": metrics_reachable,
                "coredns_pods_ready": pods_ready,
                "coredns_pod_count": len(dns_pods),
            }

            verified = rules_installed and pods_ready
            return verified, details

        def check() -> tuple[bool, dict[str, Any]]:
            try:
                return evaluate()
            except NodeFilterError as exc:
                return False, {"target_devices": devices, "error": str(exc)}

        return self.poll_verify(check)
