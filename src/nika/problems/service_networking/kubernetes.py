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
from nika.problems.support.kubernetes.objects import (
    DEFAULT_PROBE_NAMESPACE,
    edit_spec,
    restore_spec,
    selected_service,
)

from nika.problems.base import (
    FailureDomain,
)
from nika.utils.logger import system_logger

logger = system_logger

DEFAULT_PROBE_SERVICE = "kubernetes"


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


class ServiceTargetPortMismatchParams(K8sParams):
    """Parameters for pointing a Service at a port its Pods do not serve."""

    service_name: str = Field(
        default="",
        description=(
            "Service to change; defaults to the first Ingress backend or "
            "InferencePool endpoint-picker Service."
        ),
    )
    namespace: str = Field(default="", description="Namespace of `service_name`.")


class ServiceTargetPortMismatch(K8sProblemBase):
    """A Service's ``targetPort`` no longer matches the port its Pods serve.

    The Service keeps its selector, so its endpoints list every Ready Pod,
    but each endpoint carries the wrong port and connections to it are
    refused. Traffic that reaches the Service through its ClusterIP or an
    Ingress fails; the Pods themselves answer on their own port. See
    "Debug Services" in the Kubernetes documentation:
    https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/
    """

    failure_domain = FailureDomain.SERVICE_NETWORKING
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name = "k8s_service_target_port_mismatch"
    description = "A Kubernetes Service forwards to a port its Pods do not serve."
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s"]
    Params = ServiceTargetPortMismatchParams
    RECORDED_ATTRS = (*K8sProblemBase.RECORDED_ATTRS, "service_ref")
    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"control_node": k8s_control_node(ctx)}

    def __init__(self, scenario_name: str | None = None, **kwargs: Any) -> None:
        super().__init__(scenario_name, **kwargs)
        self.service_ref: str | None = None

    def _service(self, params: ServiceTargetPortMismatchParams) -> tuple[str, str]:
        return selected_service(
            self.runtime.lab_api,
            self.control_node(params),
            params.service_name,
            params.namespace,
        )

    def root_cause_resources(self, params: ServiceTargetPortMismatchParams):
        # The Service object is not a catalog resource; the cluster that
        # serves it is localized at its control-plane node.
        control = params.control_node or self.control_node(params)
        if not control:
            raise UnresolvedRootCauseError("No Kubernetes control-plane node.")
        return [node_resource(control)]

    def inject_fault(self, params: ServiceTargetPortMismatchParams):
        k8s = self.runtime.lab_api
        control = self.control_node(params)
        namespace, name = self._service(params)
        service = k8s.kubectl_json(control, f"get service {name} -n {namespace}")
        self._original = service
        port = service["spec"]["ports"][0]
        target = _as_port(port.get("targetPort"))
        used = set()
        if target is None:
            selector = service["spec"].get("selector") or {}
            if not selector:
                raise ValueError(f"Service {namespace}/{name} has no selector.")
            labels = ",".join(
                f"{key}={value}" for key, value in sorted(selector.items())
            )
            pods = k8s.kubectl_json(control, f"get pods -n {namespace} -l {labels}")
            used.update(
                int(item["containerPort"])
                for pod in pods.get("items", [])
                for container in pod["spec"].get("containers", [])
                for item in container.get("ports", [])
            )
            target = int(port["port"])
        else:
            used.add(target)
        # The common slip: the Service port typed where the Pod port belongs.
        wrong = int(port["port"]) if int(port["port"]) != target else target + 1
        while wrong in used or wrong > 65535:
            wrong = wrong % 65535 + 1

        def edit(spec: dict) -> None:
            spec["ports"][0]["targetPort"] = wrong

        edit_spec(k8s, control, service, edit)
        self.service_ref = f"{namespace}/{name}"
        self.wrong_target_port = wrong
        self.record_k8s_object("service", name, namespace=namespace)
        logger.info(f"Set targetPort {wrong} on Service {self.service_ref}.")

    def _target_port(self, params) -> int | str:
        namespace, name = (self.service_ref or "/").split("/", 1)
        value = self.runtime.lab_api.kubectl_jsonpath(
            self.control_node(params),
            f"service {name}",
            "{.spec.ports[0].targetPort}",
            namespace=namespace,
        )
        parsed = _as_port(value)
        return parsed if parsed is not None else value

    def verify_fault(self, params: ServiceTargetPortMismatchParams) -> dict:
        wrong = getattr(self, "wrong_target_port", None)
        return self.poll_verify(
            lambda: (
                wrong is not None and self._target_port(params) == wrong,
                {"service": self.service_ref, "wrong_target_port": wrong},
            )
        )

    def recover_fault(self, params: ServiceTargetPortMismatchParams) -> dict:
        from nika.validation.symptom.k8s_probes import target_port_baseline

        original = getattr(self, "_original", None)
        if not original:
            return {"verified": False, "details": {"error": "No original Service."}}
        restore_spec(self.runtime.lab_api, self.control_node(params), original)
        port = original["spec"]["ports"][0]["targetPort"]

        def check():
            healthy, details = target_port_baseline(self, params)
            return self._target_port(params) == port and healthy, {
                "service": self.service_ref,
                **details,
            }

        return self.poll_verify(check)


class ServiceSelectorMismatchParams(K8sParams):
    """Parameters for pointing a Service selector away from its Pods' labels."""

    service_name: str = Field(
        default="",
        description=(
            "Service to change; defaults to the first Ingress backend or "
            "InferencePool endpoint-picker Service."
        ),
    )
    namespace: str = Field(default="", description="Namespace of `service_name`.")


class ServiceSelectorMismatch(K8sProblemBase):
    """A Service's selector no longer matches the labels on its Pods.

    A Deployment rollout can rename a label without updating the Service, so
    the Service has no endpoints. Traffic to it fails; the Pods stay Running
    and Ready. See "Debug Services" in the Kubernetes documentation:
    https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/
    """

    failure_domain = FailureDomain.SERVICE_NETWORKING
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name = "k8s_service_selector_mismatch"
    description = "A Kubernetes Service selector no longer matches its Pods' labels."
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s"]
    Params = ServiceSelectorMismatchParams
    RECORDED_ATTRS = (*K8sProblemBase.RECORDED_ATTRS, "service_ref")
    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"control_node": k8s_control_node(ctx)}

    def __init__(self, scenario_name: str | None = None, **kwargs: Any) -> None:
        super().__init__(scenario_name, **kwargs)
        self.service_ref: str | None = None

    def _service(self, params: ServiceSelectorMismatchParams) -> tuple[str, str]:
        return selected_service(
            self.runtime.lab_api,
            self.control_node(params),
            params.service_name,
            params.namespace,
        )

    def root_cause_resources(self, params: ServiceSelectorMismatchParams):
        control = params.control_node or self.control_node(params)
        if not control:
            raise UnresolvedRootCauseError("No Kubernetes control-plane node.")
        return [node_resource(control)]

    def inject_fault(self, params: ServiceSelectorMismatchParams):
        k8s = self.runtime.lab_api
        control = self.control_node(params)
        namespace, name = self._service(params)
        service = k8s.kubectl_json(control, f"get service {name} -n {namespace}")
        self._original = service
        selector = dict((service.get("spec") or {}).get("selector") or {})
        if not selector:
            raise ValueError(f"Service {namespace}/{name} has no selector.")
        key = min(selector)
        wrong = {**selector, key: f"{selector[key]}-v2"}

        def edit(spec: dict) -> None:
            spec["selector"] = dict(wrong)

        edit_spec(k8s, control, service, edit)
        self.service_ref = f"{namespace}/{name}"
        self.wrong_selector = wrong
        self.record_k8s_object("service", name, namespace=namespace)
        logger.info(f"Updated selector on Service {self.service_ref}.")

    def _selector(self, params) -> dict[str, str]:
        namespace, name = (self.service_ref or "/").split("/", 1)
        service = self.runtime.lab_api.kubectl_json(
            self.control_node(params), f"get service {name} -n {namespace}"
        )
        return dict((service.get("spec") or {}).get("selector") or {})

    def verify_fault(self, params: ServiceSelectorMismatchParams) -> dict:
        wrong = getattr(self, "wrong_selector", None)
        return self.poll_verify(
            lambda: (
                wrong is not None and self._selector(params) == wrong,
                {"service": self.service_ref, "selector": wrong},
            )
        )

    def recover_fault(self, params: ServiceSelectorMismatchParams) -> dict:
        from nika.validation.symptom.k8s_probes import target_port_baseline

        original = getattr(self, "_original", None)
        if not original:
            return {"verified": False, "details": {"error": "No original Service."}}
        restore_spec(self.runtime.lab_api, self.control_node(params), original)

        def check():
            healthy, details = target_port_baseline(self, params)
            restored = self._selector(params) == original["spec"]["selector"]
            return restored and healthy, {"service": self.service_ref, **details}

        return self.poll_verify(check)


class LoadBalancerAdvertisementMissingParams(K8sParams):
    """Parameters for leaving MetalLB's address pool without an advertisement."""


METALLB_NAMESPACE = "metallb-system"


class LoadBalancerAdvertisementMissing(K8sProblemBase):
    """MetalLB's advertisement names an address pool that does not exist.

    MetalLB still assigns LoadBalancer IPs from the real pool, but announces
    an address only for pools that a BGPAdvertisement or L2Advertisement
    selects, so every speaker withdraws the service addresses. BGP sessions,
    when present, stay up; in L2 mode the speakers stop answering ARP.
    Clients outside the cluster lose the LoadBalancer IPs. See MetalLB's
    "Configuration" guide:
    https://metallb.universe.tf/configuration/
    """

    failure_domain = FailureDomain.SERVICE_NETWORKING
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name = "lb_advertisement_missing"
    description = "The load balancer announces no route for its service addresses."
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s", "metallb"]
    Params = LoadBalancerAdvertisementMissingParams
    BENCHMARK_TARGETS = "canonical"
    # Pool name an advertisement references after a rename that missed it.
    UNDEFINED_POOL = "ingress-pool"
    _ADVERTISEMENTS = (
        ("bgpadvertisements", "BGPAdvertisement"),
        ("l2advertisements", "L2Advertisement"),
    )

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"control_node": k8s_control_node(ctx)}

    def root_cause_resources(self, params: LoadBalancerAdvertisementMissingParams):
        control = params.control_node or self.control_node(params)
        if not control:
            raise UnresolvedRootCauseError("No Kubernetes control-plane node.")
        return [node_resource(control)]

    def _advertisements(self, params) -> list[dict[str, Any]]:
        k8s = self.runtime.lab_api
        control = self.control_node(params)
        items = []
        for resource, kind in self._ADVERTISEMENTS:
            payload = k8s.kubectl_json(
                control, f"get {resource} -n {METALLB_NAMESPACE}"
            )
            for item in payload.get("items", []):
                item["kind"] = item.get("kind") or kind
                items.append(item)
        return items

    def _pools(self, params) -> set[str]:
        items = self.runtime.lab_api.kubectl_json(
            self.control_node(params), f"get ipaddresspools -n {METALLB_NAMESPACE}"
        ).get("items", [])
        return {item["metadata"]["name"] for item in items}

    def inject_fault(self, params: LoadBalancerAdvertisementMissingParams):
        k8s = self.runtime.lab_api
        control = self.control_node(params)
        advertisements = self._advertisements(params)
        if not advertisements:
            raise ValueError("MetalLB has no advertisement to change.")
        if self.UNDEFINED_POOL in self._pools(params):
            raise ValueError(f"MetalLB pool {self.UNDEFINED_POOL!r} already exists.")
        self._originals = advertisements

        def edit(spec: dict) -> None:
            spec["ipAddressPools"] = [self.UNDEFINED_POOL]
            spec.pop("ipAddressPoolSelectors", None)

        for advertisement in advertisements:
            edit_spec(k8s, control, advertisement, edit)
            self.record_k8s_object(
                advertisement["kind"].lower(),
                advertisement["metadata"]["name"],
                namespace=METALLB_NAMESPACE,
            )
        logger.info("Pointed MetalLB advertisements at an undefined pool.")

    def _advertised_pools(self, params) -> set[str]:
        pools = self._pools(params)
        advertised = set()
        for advertisement in self._advertisements(params):
            spec = advertisement.get("spec") or {}
            named = spec.get("ipAddressPools")
            if named:
                advertised |= set(named) & pools
            elif not spec.get("ipAddressPoolSelectors"):
                advertised |= pools  # no pool named or selected: every pool
            else:
                advertised.add("selected-by-label")
        return advertised

    def verify_fault(self, params: LoadBalancerAdvertisementMissingParams) -> dict:
        def check():
            advertised = self._advertised_pools(params)
            return not advertised, {"advertised_pools": sorted(advertised)}

        return self.poll_verify(check)

    def recover_fault(self, params: LoadBalancerAdvertisementMissingParams) -> dict:
        from nika.validation.symptom.k8s_probes import lb_advertisement_baseline

        originals = getattr(self, "_originals", [])
        if not originals:
            return {"verified": False, "details": {"error": "No saved advertisements."}}
        control = self.control_node(params)
        for advertisement in originals:
            restore_spec(self.runtime.lab_api, control, advertisement)

        def check():
            advertised = self._advertised_pools(params)
            healthy, details = lb_advertisement_baseline(self, params)
            return bool(advertised) and healthy, {
                "pools": sorted(advertised),
                **details,
            }

        return self.poll_verify(check)


class IngressClassMismatch(K8sProblemBase):
    """An Ingress references an IngressClass that no longer exists.

    A controller migration can rename the class without updating an Ingress.
    The controller ignores its routes, returning its default 404, while the
    backend Services and Pods remain healthy. Kubernetes defines the class
    reference in its IngressClass documentation:
    https://kubernetes.io/docs/concepts/services-networking/ingress/#ingress-class
    """

    failure_domain = FailureDomain.SERVICE_NETWORKING
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name = "k8s_ingress_class_mismatch"
    description = "A Kubernetes Ingress references an undefined IngressClass."
    symptom_desc = "Requests for application paths return HTTP 404."
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s", "ingress"]
    Params = K8sParams
    BENCHMARK_TARGETS = "canonical"
    RECORDED_ATTRS = (*K8sProblemBase.RECORDED_ATTRS, "ingress_ref")

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"control_node": k8s_control_node(ctx)}

    def __init__(self, scenario_name: str | None = None, **kwargs: Any) -> None:
        super().__init__(scenario_name, **kwargs)
        self.ingress_ref: str | None = None

    def root_cause_resources(self, params: K8sParams):
        control = params.control_node or self.control_node(params)
        if not control:
            raise UnresolvedRootCauseError("No Kubernetes control-plane node.")
        return [node_resource(control)]

    def inject_fault(self, params: K8sParams) -> None:
        k8s = self.runtime.lab_api
        control = self.control_node(params)
        items = k8s.kubectl_json(control, "get ingress -A").get("items", [])
        if not items:
            raise ValueError("No Ingress to change.")
        ingress = min(
            items,
            key=lambda obj: (obj["metadata"]["namespace"], obj["metadata"]["name"]),
        )
        # A list response omits the individual object's kind.
        ingress["kind"] = "Ingress"
        self._original = ingress
        self.ingress_ref = (
            f"{ingress['metadata']['namespace']}/{ingress['metadata']['name']}"
        )
        classes = {
            obj["metadata"]["name"]
            for obj in k8s.kubectl_json(control, "get ingressclass").get("items", [])
        }
        wrong = "nginx-migrated"
        while wrong in classes:
            wrong += "-old"
        self.undefined_class = wrong

        def edit(spec: dict) -> None:
            spec["ingressClassName"] = wrong

        edit_spec(k8s, control, ingress, edit)
        self.record_k8s_object(
            "ingress",
            ingress["metadata"]["name"],
            namespace=ingress["metadata"]["namespace"],
        )
        logger.info(f"Set ingressClassName {wrong} on Ingress {self.ingress_ref}.")

    def _ingress(self, params: K8sParams) -> dict[str, Any]:
        namespace, name = (self.ingress_ref or "/").split("/", 1)
        return self.runtime.lab_api.kubectl_json(
            self.control_node(params), f"get ingress {name} -n {namespace}"
        )

    def verify_fault(self, params: K8sParams) -> dict:
        def check():
            value = self._ingress(params)["spec"].get("ingressClassName")
            classes = self.runtime.lab_api.kubectl_json(
                self.control_node(params), "get ingressclass"
            ).get("items", [])
            missing = value not in {obj["metadata"]["name"] for obj in classes}
            return value == self.undefined_class and missing, {
                "ingress": self.ingress_ref,
                "ingress_class": value,
                "class_undefined": missing,
            }

        return self.poll_verify(check)

    def recover_fault(self, params: K8sParams) -> dict:
        from nika.validation.symptom.k8s_probes import ingress_class_baseline

        original = getattr(self, "_original", None)
        if not original:
            return {"verified": False, "details": {"error": "No saved Ingress."}}
        restore_spec(self.runtime.lab_api, self.control_node(params), original)
        healthy, details = ingress_class_baseline(self, params)
        restored = self._ingress(params)["spec"].get("ingressClassName") == original[
            "spec"
        ].get("ingressClassName")
        return {
            "verified": restored and healthy,
            "details": {"ingress": self.ingress_ref, **details},
        }
