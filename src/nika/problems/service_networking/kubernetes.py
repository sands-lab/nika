"""Kubernetes service-forwarding failure implementations."""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from typing import Any, ClassVar

from pydantic import Field

from nika.problems.rca import (
    UnresolvedRootCauseError,
    node_resource,
)

from nika.problems.support.benchmark_targets import choice, k8s_control_node
from nika.problems.support.kubernetes.base import K8sParams, K8sProblemBase

from nika.problems.support.kubernetes.node_filter import NodeFilter, NodeFilterError

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


LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


def _object_ref(obj: dict[str, Any]) -> str:
    meta = obj["metadata"]
    scope = f" -n {meta['namespace']}" if meta.get("namespace") else ""
    return f"{obj['kind'].lower()} {meta['name']}{scope}"


def edit_spec(
    k8s: Any, control: str, obj: dict[str, Any], edit: Callable[[dict], None]
) -> None:
    """Apply ``edit`` to the live spec and to the last-applied manifest.

    The annotation then shows the edited manifest an operator would have
    applied, not the server-filled live object.
    """
    spec = json.loads(json.dumps(obj["spec"]))
    edit(spec)
    _patch_spec(k8s, control, obj, obj["spec"], spec)
    applied = (obj["metadata"].get("annotations") or {}).get(LAST_APPLIED)
    if applied:
        manifest = json.loads(applied)
        edit(manifest.setdefault("spec", {}))
        _annotate(k8s, control, obj, json.dumps(manifest, separators=(",", ":")))


def restore_spec(k8s: Any, control: str, obj: dict[str, Any]) -> None:
    """Return the live spec and the last-applied annotation to ``obj``'s."""
    live = k8s.kubectl_json(control, f"get {_object_ref(obj)}")
    _patch_spec(k8s, control, obj, live["spec"], obj["spec"])
    _annotate(
        k8s, control, obj, (obj["metadata"].get("annotations") or {}).get(LAST_APPLIED)
    )


def _patch_spec(k8s, control, obj, current: dict, wanted: dict) -> None:
    # A merge patch keeps keys it does not name; null the ones to drop.
    patch = {"spec": {**dict.fromkeys(set(current) - set(wanted)), **wanted}}
    k8s.kubectl(
        control,
        f"patch {_object_ref(obj)} --type=merge -p {shlex.quote(json.dumps(patch))}",
    )


def _annotate(k8s, control, obj, value: str | None) -> None:
    setting = f"{LAST_APPLIED}={value}" if value is not None else f"{LAST_APPLIED}-"
    k8s.kubectl(
        control, f"annotate {_object_ref(obj)} --overwrite {shlex.quote(setting)}"
    )


def ingress_backends(k8s: Any, control: str) -> list[tuple[str, str, str, str]]:
    """``(namespace, service, host, path)`` for each Ingress HTTP backend, sorted."""
    backends = []
    for item in k8s.kubectl_json(control, "get ingress -A").get("items", []):
        namespace = item["metadata"]["namespace"]
        for rule in item["spec"].get("rules", []):
            for path in (rule.get("http") or {}).get("paths", []):
                service = ((path.get("backend") or {}).get("service") or {}).get("name")
                if service:
                    backends.append(
                        (
                            namespace,
                            service,
                            rule.get("host", ""),
                            path.get("path", "/"),
                        )
                    )
    return sorted(backends)


class ServiceTargetPortMismatchParams(K8sParams):
    """Parameters for pointing a Service at a port its Pods do not serve."""

    service_name: str = Field(
        default="",
        description="Service to change; defaults to the first Service behind an Ingress.",
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
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s", "ingress"]
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
        if params.service_name:
            return params.namespace or DEFAULT_PROBE_NAMESPACE, params.service_name
        backends = ingress_backends(self.runtime.lab_api, self.control_node(params))
        if not backends:
            raise ValueError("No Ingress backend Service to change.")
        return backends[0][0], backends[0][1]

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
        target = _as_port(port.get("targetPort")) or int(port["port"])
        # The common slip: the Service port typed where the Pod port belongs.
        wrong = int(port["port"]) if int(port["port"]) != target else target + 1

        def edit(spec: dict) -> None:
            spec["ports"][0]["targetPort"] = wrong

        edit_spec(k8s, control, service, edit)
        self.service_ref = f"{namespace}/{name}"
        self.wrong_target_port = wrong
        self.record_k8s_object("service", name, namespace=namespace)
        logger.info(f"Set targetPort {wrong} on Service {self.service_ref}.")

    def _target_port(self, params) -> int | None:
        namespace, name = (self.service_ref or "/").split("/", 1)
        value = self.runtime.lab_api.kubectl_jsonpath(
            self.control_node(params),
            f"service {name}",
            "{.spec.ports[0].targetPort}",
            namespace=namespace,
        )
        return _as_port(value)

    def verify_fault(self, params: ServiceTargetPortMismatchParams) -> dict:
        wrong = getattr(self, "wrong_target_port", None)
        return self.poll_verify(
            lambda: (
                wrong is not None and self._target_port(params) == wrong,
                {"service": self.service_ref, "wrong_target_port": wrong},
            )
        )

    def recover_fault(self, params: ServiceTargetPortMismatchParams) -> dict:
        original = getattr(self, "_original", None)
        if original:
            restore_spec(self.runtime.lab_api, self.control_node(params), original)
        port = original["spec"]["ports"][0]["targetPort"] if original else None
        restored = original is not None and self._target_port(params) == _as_port(port)
        return {"verified": restored, "details": {"service": self.service_ref}}


class LoadBalancerAdvertisementMissingParams(K8sParams):
    """Parameters for leaving MetalLB's address pool without a BGP advertisement."""


METALLB_NAMESPACE = "metallb-system"


class LoadBalancerAdvertisementMissing(K8sProblemBase):
    """MetalLB's BGP advertisement names an address pool that does not exist.

    MetalLB still assigns LoadBalancer IPs from the real pool, but announces
    an address only for pools that a BGPAdvertisement selects, so every
    speaker withdraws the service addresses while its BGP sessions stay up.
    Clients outside the cluster lose the LoadBalancer IPs. See MetalLB's
    "Configuration" guide on BGPAdvertisement:
    https://metallb.universe.tf/configuration/
    """

    failure_domain = FailureDomain.SERVICE_NETWORKING
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name = "lb_advertisement_missing"
    description = "The load balancer announces no route for its service addresses."
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s", "metallb", "bgp"]
    Params = LoadBalancerAdvertisementMissingParams
    BENCHMARK_TARGETS = "canonical"
    # Pool name an advertisement references after a rename that missed it.
    UNDEFINED_POOL = "ingress-pool"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"control_node": k8s_control_node(ctx)}

    def root_cause_resources(self, params: LoadBalancerAdvertisementMissingParams):
        control = params.control_node or self.control_node(params)
        if not control:
            raise UnresolvedRootCauseError("No Kubernetes control-plane node.")
        return [node_resource(control)]

    def _advertisements(self, params) -> list[dict[str, Any]]:
        return self.runtime.lab_api.kubectl_json(
            self.control_node(params), f"get bgpadvertisements -n {METALLB_NAMESPACE}"
        ).get("items", [])

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
            raise ValueError("MetalLB has no BGPAdvertisement to change.")
        self._originals = advertisements

        def edit(spec: dict) -> None:
            spec["ipAddressPools"] = [self.UNDEFINED_POOL]
            spec.pop("ipAddressPoolSelectors", None)

        for advertisement in advertisements:
            edit_spec(k8s, control, advertisement, edit)
            self.record_k8s_object(
                "bgpadvertisement",
                advertisement["metadata"]["name"],
                namespace=METALLB_NAMESPACE,
            )
        logger.info("Pointed MetalLB BGP advertisements at an undefined pool.")

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
        control = self.control_node(params)
        for advertisement in getattr(self, "_originals", []):
            restore_spec(self.runtime.lab_api, control, advertisement)
        advertised = self._advertised_pools(params)
        return {"verified": bool(advertised), "details": {"pools": sorted(advertised)}}
