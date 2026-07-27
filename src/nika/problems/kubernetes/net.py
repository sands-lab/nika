"""Kubernetes service-networking faults (ClusterIP, kube-proxy, DNS, ingress)."""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from nika.problems.kubernetes.base import K8sParams, K8sProblemBase
from nika.problems.kubernetes.node_filter import DropSpec, NodeFilter, NodeFilterError
from nika.problems.problem_base import RootCauseCategory
from nika.utils.logger import system_logger

logger = system_logger

#: Probed when the fault blocks the whole Service CIDR: the ``kubernetes``
#: Service exists in every cluster and its endpoint is the apiserver, so the
#: "ClusterIP fails / direct address works" contrast can be checked in any lab.
DEFAULT_PROBE_SERVICE = "kubernetes"
DEFAULT_PROBE_NAMESPACE = "default"

#: CoreDNS in k3s: Deployment ``coredns`` behind Service ``kube-dns``.
DNS_SERVICE = "kube-dns"
DNS_NAMESPACE = "kube-system"
DNS_SELECTOR = "k8s-app=kube-dns"
DNS_PORT = 53
DNS_PROTOCOLS = ("udp", "tcp")
#: CoreDNS metrics port: left reachable on purpose, so verification can show
#: that the CoreDNS pod is up and routable while only DNS itself is cut.
DNS_METRICS_PORT = 9153


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
    root_cause_category: RootCauseCategory = RootCauseCategory.MISCONFIGURATION
    root_cause_name: str = "k8s_coredns_isolated"
    symptom_desc = (
        "Applications cannot resolve Kubernetes service names such as "
        "*.svc.cluster.local and report DNS timeouts, while communication by IP "
        "address keeps working. The CoreDNS pods are Running and Ready and the DNS "
        "Service still lists its endpoints."
    )
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s", "coredns"]
    FAULTY_DEVICE_POLICY: ClassVar[str] = "affected_nodes"

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

    def _dns_destinations(self, params: CoreDNSIsolationParams, k8s: Any) -> dict[str, Any]:
        control = self.control_node(params)
        cluster_ip = k8s.k8s_service_cluster_ip(control, params.dns_service, namespace=params.dns_namespace)
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
    def _drop_specs(params: CoreDNSIsolationParams, destinations: dict[str, Any]) -> list[DropSpec]:
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

    def inject_fault(self, params: CoreDNSIsolationParams) -> None:
        k8s = self.runtime.lab_api
        devices = self._target_devices(params, k8s)
        destinations = self._dns_destinations(params, k8s)
        specs = self._drop_specs(params, destinations)

        self.set_faulty_devices(self.faulty_devices_for(params, affected=devices))
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
            dns_reachable = probe.tcp_reachable(destinations["cluster_ip"], params.dns_port)
            metrics_reachable = probe.tcp_reachable(pod_ips[0], DNS_METRICS_PORT) if pod_ips else None

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


class ClusterIPRoutingBroken(K8sProblemBase):
    root_cause_category: RootCauseCategory = RootCauseCategory.NETWORK_NODE_ERROR
    root_cause_name: str = "k8s_clusterip_routing_broken"
    symptom_desc = (
        "Pods scheduled on one Kubernetes node cannot reach any ClusterIP Service, "
        "including in-cluster DNS, while direct pod-IP traffic from the same node "
        "still works. Services, endpoints and pods all report healthy, and the node "
        "stays Ready."
    )
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s", "kube_proxy"]
    FAULTY_DEVICE_POLICY: ClassVar[str] = "affected_nodes"

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

    def _probe_plan(self, params: ClusterIPRoutingBrokenParams, k8s: Any) -> dict[str, Any]:
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

    def inject_fault(self, params: ClusterIPRoutingBrokenParams) -> None:
        k8s = self.runtime.lab_api
        device = self._target_device(params)
        target = self._block_target(params, k8s)
        self.blocked_target = target

        self.set_faulty_devices(self.faulty_devices_for(params, affected=[device]))
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
                clusterip_reachable = node_filter.tcp_reachable(cluster_ip, service_port)
            if backend_address and backend_port:
                backend_reachable = node_filter.tcp_reachable(backend_address, backend_port)

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
