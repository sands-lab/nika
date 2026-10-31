"""Kubernetes service-forwarding failure implementations."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from pydantic import Field

from nika.problems.rca import (
    UnresolvedRootCauseError,
    node_resource,
)

from nika.problems.support.benchmark_targets import choice, k8s_control_node
from nika.problems.support.kubernetes.base import K8sParams, K8sProblemBase

from nika.problems.support.kubernetes.node_filter import NodeFilter, NodeFilterError
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    OracleStep,
    contains,
    kubectl,
    shell,
    tcp_connect_cmd,
)

from nika.problems.base import (
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
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name: str = "k8s_clusterip_routing_broken"
    description = "Kubernetes ClusterIP forwarding is broken on a node."
    symptom_desc = (
        "Pods scheduled on one Kubernetes node cannot reach any ClusterIP Service, "
        "including in-cluster DNS, while direct pod-IP traffic from the same node "
        "still works. Services, endpoints and pods all report healthy, and the node "
        "stays Ready."
    )
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s", "kube_proxy"]

    Params = ClusterIPRoutingBrokenParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        k8s_nodes = ctx.roles.get("k8s_nodes") or []
        control = k8s_control_node(ctx)
        workers = sorted(node for node in k8s_nodes if node != control)
        return {"control_node": control, "node_name": choice(ctx.rng, workers, control)}

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

    def oracle_playbook(self, params: ClusterIPRoutingBrokenParams) -> OraclePlaybook:
        control = self.control_node(params)
        node = params.node_name or control
        peer = next(
            (n for n in sorted(self.cluster_nodes()) if n not in {node, control}),
            control,
        )
        if params.service_name:
            namespace = params.namespace or DEFAULT_PROBE_NAMESPACE
            service = params.service_name
            scope = f"Service {namespace}/{service}"
        else:
            namespace, service = DEFAULT_PROBE_NAMESPACE, DEFAULT_PROBE_SERVICE
            scope = "the Service CIDR"
        single_service = bool(params.service_name)

        def raw_rules(outputs: Mapping[str, str]) -> dict[str, Any]:
            match = re.search(r"^clusterip (\S+):\d+$", outputs.get("svc", ""), re.M)
            cluster_ip = match.group(1) if match else "VIP_UNKNOWN"
            return {
                "host_name": node,
                "command": f"echo clusterip {cluster_ip}; iptables -t raw -S 2>&1 "
                "| grep -E -- '-j DROP|^iptables' | head -20",
                "timeout": 15,
            }

        def drops_cluster_ip(output: str) -> bool:
            match = re.search(r"^clusterip (\S+)$", output, re.M)
            try:
                cluster_ip = ipaddress.ip_address(match.group(1)) if match else None
            except ValueError:
                return False
            if cluster_ip is None:
                return False
            chains: set[str] = set()
            for chain, dest in re.findall(
                r"^-A (PREROUTING|OUTPUT) -d (\S+) -j DROP$", output, re.M
            ):
                net = ipaddress.ip_network(dest, strict=False)
                # One Service: a host drop on its ClusterIP. Whole CIDR: a
                # wider drop that contains the probed ClusterIP.
                if cluster_ip in net and (net.prefixlen == 32) == single_service:
                    chains.add(chain)
            return chains == {"PREROUTING", "OUTPUT"}

        def probe(host: str) -> Callable[[Mapping[str, str]], dict[str, Any]]:
            def args(outputs: Mapping[str, str]) -> dict[str, Any]:
                found = dict(
                    re.findall(
                        r"^(clusterip|endpoint) (\S+)$", outputs.get("svc", ""), re.M
                    )
                )
                commands = [
                    f"echo {kind} {target} $({tcp_connect_cmd(*target.rsplit(':', 1))})"
                    for kind, target in found.items()
                    if ":" in target
                ]
                return {
                    "host_name": host,
                    "command": "; ".join(commands) or "echo NO_SERVICE",
                    "timeout": 20,
                }

            return args

        return OraclePlaybook(
            finding=(
                f"Packet filter rules on {node} drop traffic to {scope} ahead of "
                "kube-proxy, so ClusterIP Services fail from that node while their "
                "endpoints, the Service objects, and the node itself stay healthy."
            ),
            steps=[
                kubectl(
                    control,
                    "get nodes --no-headers",
                    "Check node health: the affected node is Ready.",
                    contains(rf"^{node}\s+Ready\b", f"{node} is Ready"),
                ),
                shell(
                    control,
                    f"echo clusterip $(kubectl get svc {service} -n {namespace} "
                    "-o jsonpath='{.spec.clusterIP}:{.spec.ports[0].port}'); "
                    f"echo endpoint $(kubectl get endpointslices -n {namespace} "
                    f"-l kubernetes.io/service-name={service} -o jsonpath="
                    "'{.items[0].endpoints[0].addresses[0]}:{.items[0].ports[0].port}')",
                    f"Read the {namespace}/{service} ClusterIP and its endpoint: the "
                    "Service object and endpoints are intact.",
                    contains(
                        r"^clusterip \S+:\d+$(?s:.*)^endpoint \S+:\d+$",
                        "the Service has a ClusterIP and an endpoint",
                    ),
                    timeout=30,
                    save_as="svc",
                ),
                OracleStep(
                    f"From {node}, open TCP to the ClusterIP and to its endpoint.",
                    "exec_shell",
                    probe(node),
                    contains(
                        r"^clusterip \S+ NO_CONNECT$(?s:.*)^endpoint \S+ CONNECTED$",
                        "the ClusterIP fails while its endpoint accepts TCP",
                    ),
                    evidence=True,
                    attempts=2,
                ),
                OracleStep(
                    f"From {peer}, open the same ClusterIP: other nodes still reach it.",
                    "exec_shell",
                    probe(peer),
                    contains(
                        r"^clusterip \S+ CONNECTED$",
                        "the ClusterIP works from another node",
                    ),
                    evidence=True,
                ),
                OracleStep(
                    f"Inspect the raw-table packet filter on {node}, which runs before "
                    "kube-proxy's Service DNAT.",
                    "exec_shell",
                    raw_rules,
                    Check(
                        f"raw PREROUTING and OUTPUT drop {scope}, covering the probed "
                        "ClusterIP",
                        drops_cluster_ip,
                    ),
                    evidence=True,
                ),
            ],
        )

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

    def recheck_artifact(self, params: ClusterIPRoutingBrokenParams) -> dict:
        """Re-read the raw-table drops. Does not open a TCP connection."""
        k8s = self.runtime.lab_api
        device = self._target_device(params)
        target = self.blocked_target or self._block_target(params, k8s)
        blocked = NodeFilter(self.runtime, device).blocked(target)
        present = bool(blocked.get("prerouting")) and bool(blocked.get("output"))
        return {
            "present": present,
            "fault": self.root_cause_name,
            "scope": "artifact",
            "evidence": {
                "target_device": device,
                "blocked_target": target,
                "raw_prerouting_blocked": bool(blocked.get("prerouting")),
                "raw_output_blocked": bool(blocked.get("output")),
            },
            "error": None if present else "fault artifact absent",
        }
