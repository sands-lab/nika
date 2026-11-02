from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from pydantic import Field

from nika.problems.rca import UnresolvedRootCauseError, node_resource
from nika.problems.support.benchmark_targets import choice, k8s_control_node
from nika.problems.support.kubernetes.objects import (
    edit_spec,
    restore_spec,
    selected_service,
    traffic_backends,
)
from nika.problems.support.kubernetes.base import (
    K8sParams,
    K8sProblemBase,
    control_node_from_net_env,
)
from nika.problems.support.kubernetes.node_filter import (
    DropSpec,
    NodeFilter,
    NodeFilterError,
)
from nika.problems.base import (
    FailureDomain,
)
from nika.problems.support.oracle import (
    OraclePlaybook,
    OracleStep,
    contains,
    shell,
    tcp_connect_cmd,
)
from nika.utils.logger import system_logger

logger = system_logger

APISERVER_PORT = 6443

#: The kubelet is marked NotReady only after the controller manager's
#: node-monitor-grace-period (~40s by default), so verification needs a longer
#: budget than the class default.
NODE_NOTREADY_TIMEOUT_SEC: float = 240.0
LOGS_REQUEST_TIMEOUT_SEC = 10


class WorkerApiServerPartitionParams(K8sParams):
    """Parameters for partitioning a worker node from the API server."""

    node_name: str = Field(
        default="",
        description=(
            "Worker device to cut off from the API server. "
            "Defaults to the first non-control-plane cluster node."
        ),
    )
    apiserver_port: int = Field(
        default=APISERVER_PORT,
        description="API server / k3s supervisor port to block (k3s uses 6443).",
    )
    apiserver_address: str = Field(
        default="",
        description=(
            "Single API server address to block. When empty, blocks TCP/6443 to "
            "the control-plane eth0 address and Kubernetes Node InternalIP so the "
            "k3s agent cannot fail over between those paths."
        ),
    )


class WorkerApiServerPartition(K8sProblemBase):
    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "k8s_worker_apiserver_partition"
    description = "Worker is partitioned from the Kubernetes API server."
    symptom_desc = (
        "One Kubernetes worker node reports NotReady and stops receiving new pods, "
        "and `kubectl exec` / `kubectl logs` fail for the pods it hosts. Those pods "
        "keep running and answer on their pod IPs, but they are marked not ready and "
        "removed from Service endpoints, so services that depend only on them fail; "
        "after the eviction timeout they stay Terminating. The node itself is still "
        "reachable over the network."
    )
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s", "k8s_control_plane"]

    Params = WorkerApiServerPartitionParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        k8s_nodes = ctx.roles.get("k8s_nodes") or []
        control = k8s_control_node(ctx)
        workers = sorted(node for node in k8s_nodes if node != control)
        # Never fall back to the control plane: that inject is rejected at runtime.
        if not workers:
            raise ValueError(
                "k8s_worker_apiserver_partition requires a worker node; "
                f"{ctx.scenario} has only control-plane device {control!r}"
            )
        return {"control_node": control, "node_name": ctx.rng.choice(workers)}

    @classmethod
    def benchmark_node_targets(cls, ctx, targets):
        # Partitioning the control plane from itself is unverifiable.
        control = control_node_from_net_env(ctx.net_env)
        if control:
            targets = [target for target in targets if target != control]
        return targets

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        node_name = inject.get("node_name") or ""
        control = (
            inject.get("control_node") or control_node_from_net_env(ctx.net_env) or ""
        )
        if node_name and control and node_name == control:
            raise ValueError(
                "k8s_worker_apiserver_partition node_name must be a worker device, "
                f"not the control-plane device {control!r}"
            )

    def __init__(self, scenario_name: str | None = None, **kwargs: Any) -> None:
        super().__init__(scenario_name, **kwargs)
        self.target_device: str | None = None
        self.blocked_specs: list[str] = []

    def _target_device(self, params: WorkerApiServerPartitionParams) -> str:
        if params.node_name:
            self.target_device = params.node_name
        if self.target_device is None:
            workers = self.worker_devices(params)
            if not workers:
                raise ValueError(
                    f"{type(self).__name__} found no k3s nodes in "
                    f"{self.scenario_name!r}; cannot partition a worker."
                )
            self.target_device = workers[0]
        return self.target_device

    def _apiserver_addresses(
        self, params: WorkerApiServerPartitionParams, k8s: Any
    ) -> list[str]:
        if params.apiserver_address:
            return [params.apiserver_address]

        control = self.control_node(params)
        # Agents join via K3S_URL=https://<control>:6443 (lab eth0 /etc/hosts),
        # but after that path is cut they can fail over to the Kubernetes Node
        # InternalIP (often a docker-bridge address on eth1). Block both.
        addresses: list[str] = []
        lab_ip = self.runtime.get_host_ip(control)
        if lab_ip:
            addresses.append(str(lab_ip))

        control_node_name = k8s.k8s_node_for_device(
            control, control, devices=self.cluster_nodes()
        )
        for entry in k8s.k8s_nodes(control):
            if entry.get("name") == control_node_name and entry.get("internal_ip"):
                addresses.append(str(entry["internal_ip"]))
                break

        # Preserve order, drop duplicates.
        unique = list(dict.fromkeys(addresses))
        if not unique:
            raise ValueError(
                f"Cannot resolve the API server address for {control!r}: the node has "
                "no address on its first interface and no InternalIP. Pass "
                "--set apiserver_address=<ip>."
            )
        return unique

    def _drop_specs(
        self, params: WorkerApiServerPartitionParams, k8s: Any
    ) -> list[DropSpec]:
        return [
            DropSpec(address, protocol="tcp", port=params.apiserver_port)
            for address in self._apiserver_addresses(params, k8s)
        ]

    def root_cause_resources(self, params: WorkerApiServerPartitionParams):
        node = (params.node_name or "").strip()
        if not node:
            raise UnresolvedRootCauseError(
                "k8s_worker_apiserver_partition needs node_name for a unique resource."
            )
        return [node_resource(node)]

    def oracle_playbook(self, params: WorkerApiServerPartitionParams) -> OraclePlaybook:
        control = self.control_node(params)
        worker = self._target_device(params)
        port = params.apiserver_port
        # Workers join the API server by the control plane's lab name.
        server = params.apiserver_address or control

        def node_query(template: str) -> Callable[[Mapping[str, str]], dict[str, Any]]:
            def args(outputs: Mapping[str, str]) -> dict[str, Any]:
                match = re.search(
                    r"^node_name (\S+)$", outputs.get("node_name", ""), re.M
                )
                # An unknown name makes kubectl fail rather than match another node.
                node = match.group(1) if match else "NODE_UNKNOWN"
                return {
                    "host_name": control,
                    "command": f"kubectl {template.format(node=node)} 2>&1 | head -20",
                    "timeout": 30,
                }

            return args

        return OraclePlaybook(
            finding=(
                f"Packet filter rules on {worker} drop its TCP/{port} traffic to the "
                f"API server on {control}, so the kubelet cannot post status and the "
                "node goes NotReady while it stays up and reachable."
            ),
            steps=[
                shell(
                    worker,
                    "echo node_name $(hostname)",
                    f"Read {worker}'s hostname, which k3s registers as its node name.",
                    contains(r"^node_name \S+$", "the hostname is known"),
                    save_as="node_name",
                ),
                OracleStep(
                    f"Read the Kubernetes node status of {worker}: it is NotReady.",
                    "exec_shell",
                    node_query("get node {node} --no-headers"),
                    contains(r"^\S+\s+NotReady\b", f"{worker} is NotReady"),
                    evidence=True,
                    attempts=6,
                ),
                OracleStep(
                    f"Read {worker}'s conditions: the kubelet stopped posting status.",
                    "exec_shell",
                    node_query(
                        "get node {node} -o jsonpath='{{range .status.conditions[*]}}"
                        '{{.type}}={{.status}} {{.reason}}{{"\\n"}}{{end}}\''
                    ),
                    contains(
                        r"^Ready=Unknown NodeStatusUnknown$",
                        "Ready is Unknown because the kubelet stopped posting status",
                    ),
                ),
                shell(
                    worker,
                    "ps -eo args | grep -E '[k]3s agent' || echo NO_PROCESS",
                    f"Check that the k3s agent (kubelet) is still running on {worker}.",
                    contains(r"k3s agent", "the k3s agent process is running"),
                    evidence=True,
                ),
                shell(
                    worker,
                    f"ping -c 2 -W 1 {server} 2>&1 | grep -E 'received'; "
                    f"echo apiserver {server}:{port} $({tcp_connect_cmd(server, port)})",
                    f"From {worker}, ping the control plane and open the API server port.",
                    contains(
                        r"\b[1-9] (packets )?received(?s:.*)^apiserver \S+ NO_CONNECT$",
                        f"the control plane answers ping but TCP/{port} does not connect",
                    ),
                    evidence=True,
                    timeout=20,
                ),
                shell(
                    worker,
                    f"iptables-save 2>/dev/null | grep -E -- '--dport {port} .*-j (DROP|REJECT)' "
                    "| head -20",
                    f"Inspect the packet filter on {worker}.",
                    contains(
                        rf"-p tcp .*--dport {port} -j (DROP|REJECT)",
                        f"iptables drops TCP/{port} toward the API server",
                    ),
                    evidence=True,
                ),
            ],
        )

    def inject_fault(self, params: WorkerApiServerPartitionParams) -> None:
        k8s = self.runtime.lab_api
        device = self._target_device(params)
        control = self.control_node(params)
        if device == control:
            raise ValueError(
                f"{type(self).__name__} refuses to partition the control-plane device "
                f"{control!r} from itself: that disables kubectl and makes the fault "
                "unverifiable. Name a worker device with --set node_name=<device>."
            )

        specs = self._drop_specs(params, k8s)
        self.blocked_specs = [spec.describe() for spec in specs]
        self.record_k8s_object(
            "Node",
            k8s.k8s_node_for_device(control, device, devices=self.cluster_nodes()),
        )

        node_filter = NodeFilter(self.runtime, device)
        for spec in specs:
            node_filter.block(spec)

        logger.info(
            f"Partitioned {device} from the API server using iptables: "
            f"dropped {', '.join(self.blocked_specs)}. The kubelet can no longer post "
            "status, so the node will report NotReady while its running pods survive."
        )

    def verify_fault(self, params: WorkerApiServerPartitionParams) -> dict:
        k8s = self.runtime.lab_api
        control = self.control_node(params)
        device = self._target_device(params)
        node_name = k8s.k8s_node_for_device(
            control, device, devices=self.cluster_nodes()
        )
        specs = self._drop_specs(params, k8s)
        node_filter = NodeFilter(self.runtime, device)
        apiserver_addresses = [spec.destination for spec in specs]
        apiserver_address = apiserver_addresses[0]

        def evaluate() -> tuple[bool, dict[str, Any]]:
            unfiltered = [
                f"{spec.describe()}:{chain}"
                for spec in specs
                for chain, present in node_filter.blocked_spec(spec).items()
                if not present
            ]
            rules_installed = not unfiltered

            # The control plane still serves kubectl: only the worker's egress is
            # filtered, which is what makes the fault observable.
            node_entry = next(
                (
                    entry
                    for entry in k8s.k8s_nodes(control)
                    if entry["name"] == node_name
                ),
                None,
            )
            node_ready = bool(node_entry and node_entry.get("ready"))

            pods = k8s.k8s_pods(
                control,
                all_namespaces=True,
                field_selector=f"spec.nodeName={node_name}",
            )
            running = [pod for pod in pods if pod["phase"] == "Running"]

            # kubectl logs is tunnelled through the agent's connection to the
            # supervisor on the blocked port, so it must fail for pods here.
            logs_blocked: bool | None = None
            logs_probe_pod = ""
            if running:
                target_pod = running[0]
                logs_probe_pod = f"{target_pod['namespace']}/{target_pod['name']}"
                result = k8s.kubectl(
                    control,
                    f"logs {target_pod['name']} -n {target_pod['namespace']} --tail=1",
                    timeout=LOGS_REQUEST_TIMEOUT_SEC,
                    check=False,
                )
                logs_blocked = not result.ok

            reachable_paths = {
                address: node_filter.tcp_reachable(address, params.apiserver_port)
                for address in apiserver_addresses
            }
            apiserver_reachable = any(reachable_paths.values())
            # Ping still works: this is a port-scoped partition, not a dead node.
            control_pingable = self.runtime.ping_ok(device, apiserver_address, count=2)

            details: dict[str, Any] = {
                "target_device": device,
                "k8s_node": node_name,
                "apiserver_address": apiserver_address,
                "apiserver_addresses": apiserver_addresses,
                "apiserver_port": params.apiserver_port,
                "blocked_specs": [spec.describe() for spec in specs],
                "unfiltered": unfiltered,
                "node_ready": node_ready,
                "node_condition_source": node_entry or {},
                "pods_on_node": len(pods),
                "pods_running_on_node": len(running),
                "logs_probe_pod": logs_probe_pod,
                "kubectl_logs_blocked": logs_blocked,
                "apiserver_port_reachable": apiserver_reachable,
                "apiserver_port_reachable_by_address": reachable_paths,
                "node_still_pingable": control_pingable,
            }

            verified = rules_installed and not node_ready
            return verified, details

        def check() -> tuple[bool, dict[str, Any]]:
            try:
                return evaluate()
            except NodeFilterError as exc:
                return False, {"target_device": device, "error": str(exc)}

        return self.poll_verify(check, timeout=NODE_NOTREADY_TIMEOUT_SEC)

    def recheck_artifact(self, params: WorkerApiServerPartitionParams) -> dict:
        """Re-read the worker iptables drops. Does not ping or open kubectl logs."""
        k8s = self.runtime.lab_api
        device = self._target_device(params)
        specs = self._drop_specs(params, k8s)
        node_filter = NodeFilter(self.runtime, device)
        unfiltered = [
            f"{spec.describe()}:{chain}"
            for spec in specs
            for chain, installed in node_filter.blocked_spec(spec).items()
            if not installed
        ]
        present = not unfiltered
        return {
            "present": present,
            "fault": self.root_cause_name,
            "scope": "artifact",
            "evidence": {
                "target_device": device,
                "unfiltered": unfiltered,
            },
            "error": None if present else "fault artifact absent",
        }


_SYSTEM_NAMESPACES = frozenset(
    {
        "default",
        "ingress-nginx",
        "kube-node-lease",
        "kube-public",
        "kube-system",
        "local-path-storage",
        "metallb-system",
        "monitoring",
    }
)

TAINT_KEY = "maintenance"
TAINT_VALUE = "true"
TAINT_EFFECT = "NoExecute"


def _pod_template_labels(deploy: dict[str, Any]) -> dict[str, str]:
    return dict(
        (((deploy.get("spec") or {}).get("template") or {}).get("metadata") or {}).get(
            "labels"
        )
        or {}
    )


def _selector_matches(selector: dict[str, str], labels: dict[str, str]) -> bool:
    return bool(selector) and all(
        labels.get(key) == value for key, value in selector.items()
    )


def _deployment_for_service(
    k8s: Any, control: str, namespace: str, service_name: str
) -> dict[str, Any]:
    """Return the Deployment whose Pod template matches ``service_name``."""
    service = k8s.kubectl_json(control, f"get service {service_name} -n {namespace}")
    selector = dict((service.get("spec") or {}).get("selector") or {})
    if not selector:
        raise ValueError(f"Service {namespace}/{service_name} has no selector.")
    matched = []
    for item in k8s.kubectl_json(control, f"get deploy -n {namespace}").get(
        "items", []
    ):
        if _selector_matches(selector, _pod_template_labels(item)):
            item["kind"] = item.get("kind") or "Deployment"
            matched.append(item)
    if not matched:
        raise ValueError(
            f"No Deployment in {namespace} matches Service {service_name}'s selector."
        )
    if len(matched) != 1:
        raise ValueError(
            f"Several Deployments in {namespace} match Service {service_name}."
        )
    return matched[0]


def _deployment_object(
    k8s: Any, control: str, name: str, namespace: str
) -> dict[str, Any]:
    """Load ``name``, using ``namespace`` when set, otherwise the unique match."""
    if namespace:
        item = k8s.kubectl_json(control, f"get deploy {name} -n {namespace}")
        item["kind"] = item.get("kind") or "Deployment"
        return item
    matched = [
        item
        for item in k8s.kubectl_json(control, "get deploy -A").get("items", [])
        if item["metadata"]["name"] == name
    ]
    if not matched:
        raise ValueError(f"Deployment {name!r} not found.")
    if len(matched) > 1:
        namespaces = sorted(item["metadata"]["namespace"] for item in matched)
        raise ValueError(f"Deployment {name!r} exists in {namespaces}; pass namespace.")
    item = matched[0]
    item["kind"] = item.get("kind") or "Deployment"
    return item


def _services_for_deployment(
    k8s: Any, control: str, deploy: dict[str, Any]
) -> list[tuple[str, str]]:
    """Services in the Deployment's namespace whose selectors match its Pods."""
    namespace = deploy["metadata"]["namespace"]
    labels = _pod_template_labels(deploy)
    if not labels:
        raise ValueError(
            f"Deployment {namespace}/{deploy['metadata']['name']} has no Pod labels."
        )
    matched = []
    for item in k8s.kubectl_json(control, f"get svc -n {namespace}").get("items", []):
        selector = dict((item.get("spec") or {}).get("selector") or {})
        if _selector_matches(selector, labels):
            matched.append((namespace, item["metadata"]["name"]))
    return sorted(matched)


def _observable_service(
    k8s: Any, control: str, candidates: list[tuple[str, str]]
) -> tuple[str, str]:
    """The first candidate that sits on an Ingress or InferencePool path."""
    backends = {
        (namespace, name) for namespace, name, _url in traffic_backends(k8s, control)
    }
    observable = [pair for pair in candidates if pair in backends]
    if not observable:
        raise ValueError(
            "Deployment has no Service on an Ingress or InferencePool path."
        )
    if len(observable) > 1:
        # The symptom check expects one affected Service and the rest unchanged.
        raise ValueError(f"Deployment backs several observable Services: {observable}.")
    return observable[0]


def _local_volume_nodes(k8s: Any, control: str) -> list[str]:
    """Hostnames of local PersistentVolumes bound to a non-system claim."""
    nodes: list[str] = []
    for item in k8s.kubectl_json(control, "get pv").get("items", []):
        claim = (item.get("spec") or {}).get("claimRef") or {}
        if not claim.get("name") or claim.get("namespace") in _SYSTEM_NAMESPACES:
            continue
        terms = (
            ((item.get("spec") or {}).get("nodeAffinity") or {}).get("required") or {}
        ).get("nodeSelectorTerms") or []
        for term in terms:
            for expr in term.get("matchExpressions") or []:
                if (
                    expr.get("key") == "kubernetes.io/hostname"
                    and expr.get("operator") == "In"
                ):
                    nodes.extend(str(value) for value in expr.get("values") or [])
    return sorted(set(nodes))


def _declared_storage_nodes(net_env: Any) -> list[str]:
    return list(getattr(net_env, "kubernetes_storage_nodes", None) or [])


class DeploymentReplicasZeroParams(K8sParams):
    """Parameters for setting a Deployment's replica count to zero."""

    deployment_name: str = Field(
        default="",
        description=(
            "Deployment to change. When set, that Deployment is resolved first "
            "and must have a Service on an Ingress or InferencePool path. "
            "When omitted, uses the Deployment behind the first such Service."
        ),
    )
    namespace: str = Field(default="", description="Namespace of `deployment_name`.")


def _deployment_targets(
    problem: Any, params: DeploymentReplicasZeroParams
) -> tuple[str, str, str, str]:
    k8s = problem.runtime.lab_api
    control = problem.control_node(params)
    if params.deployment_name:
        deploy = _deployment_object(
            k8s, control, params.deployment_name, params.namespace
        )
        namespace = deploy["metadata"]["namespace"]
        name = deploy["metadata"]["name"]
        services = _services_for_deployment(k8s, control, deploy)
        if not services:
            raise ValueError(
                f"No Service in {namespace} selects Deployment {name}'s Pods."
            )
        service_ns, service = _observable_service(k8s, control, services)
        return namespace, name, service_ns, service
    namespace, service = selected_service(k8s, control, "", params.namespace)
    deploy = _deployment_for_service(k8s, control, namespace, service)
    _observable_service(k8s, control, _services_for_deployment(k8s, control, deploy))
    return namespace, deploy["metadata"]["name"], namespace, service


class DeploymentReplicasZero(K8sProblemBase):
    """A Deployment's ``spec.replicas`` is set to zero.

    An operator or a GitOps rollback can scale a workload to zero, so the
    ReplicaSet deletes every Pod. The Service keeps its selector and has no
    endpoints. Requests for that application fail; other Deployments are
    unchanged. See "Scaling a Deployment" in the Kubernetes documentation:
    https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#scaling-a-deployment
    """

    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name = "k8s_deployment_replicas_zero"
    description = "A Kubernetes Deployment is scaled to zero replicas."
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s"]
    Params = DeploymentReplicasZeroParams
    BENCHMARK_TARGETS = "canonical"
    RECORDED_ATTRS = (
        *K8sProblemBase.RECORDED_ATTRS,
        "deployment_ref",
        "service_ref",
    )

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"control_node": k8s_control_node(ctx)}

    def __init__(self, scenario_name: str | None = None, **kwargs: Any) -> None:
        super().__init__(scenario_name, **kwargs)
        self.deployment_ref: str | None = None
        self.service_ref: str | None = None

    def _targets(
        self, params: DeploymentReplicasZeroParams
    ) -> tuple[str, str, str, str]:
        return _deployment_targets(self, params)

    def root_cause_resources(self, params: DeploymentReplicasZeroParams):
        control = params.control_node or self.control_node(params)
        if not control:
            raise UnresolvedRootCauseError("No Kubernetes control-plane node.")
        return [node_resource(control)]

    def inject_fault(self, params: DeploymentReplicasZeroParams) -> None:
        k8s = self.runtime.lab_api
        control = self.control_node(params)
        namespace, name, service_ns, service = self._targets(params)
        deploy = k8s.kubectl_json(control, f"get deploy {name} -n {namespace}")
        deploy["kind"] = deploy.get("kind") or "Deployment"
        self._original = deploy
        replicas = int((deploy.get("spec") or {}).get("replicas") or 0)
        if replicas == 0:
            raise ValueError(f"Deployment {namespace}/{name} already has 0 replicas.")

        def edit(spec: dict) -> None:
            spec["replicas"] = 0

        edit_spec(k8s, control, deploy, edit)
        self.deployment_ref = f"{namespace}/{name}"
        self.service_ref = f"{service_ns}/{service}" if service else None
        self.k8s_namespace = namespace
        self.record_k8s_object("deployment", name, namespace=namespace)
        logger.info("Set replicas 0 on Deployment %s.", self.deployment_ref)

    def _replicas(self, params: DeploymentReplicasZeroParams) -> int | None:
        if not self.deployment_ref:
            return None
        namespace, name = self.deployment_ref.split("/", 1)
        value = self.runtime.lab_api.kubectl_jsonpath(
            self.control_node(params),
            f"deploy {name}",
            "{.spec.replicas}",
            namespace=namespace,
        )
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def verify_fault(self, params: DeploymentReplicasZeroParams) -> dict:
        def check():
            replicas = self._replicas(params)
            return replicas == 0, {
                "deployment": self.deployment_ref,
                "replicas": replicas,
            }

        return self.poll_verify(check)

    def recover_fault(self, params: DeploymentReplicasZeroParams) -> dict:
        from nika.validation.symptom.k8s_probes import target_port_baseline

        original = getattr(self, "_original", None)
        if not original:
            return {"verified": False, "details": {"error": "No original Deployment."}}
        k8s = self.runtime.lab_api
        control = self.control_node(params)
        restore_spec(k8s, control, original)

        def check():
            meta = original["metadata"]
            live = _deployment_object(k8s, control, meta["name"], meta["namespace"])
            healthy, details = target_port_baseline(self, params)
            return _deployment_restored(live, original) and healthy, {
                "deployment": self.deployment_ref,
                "status": live.get("status"),
                **details,
            }

        return self.poll_verify(check, timeout=900)


def _deployment_restored(live: dict, original: dict) -> bool:
    wanted = original["spec"].get("replicas", 1)
    status = live.get("status") or {}
    return (
        live["spec"] == original["spec"]
        and status.get("observedGeneration", 0) >= live["metadata"]["generation"]
        and all(
            status.get(key, 0) == wanted
            for key in (
                "replicas",
                "updatedReplicas",
                "readyReplicas",
                "availableReplicas",
            )
        )
    )


class NodeNoExecuteTaintParams(K8sParams):
    """Parameters for adding a NoExecute taint to a Kubernetes node."""

    node_name: str = Field(
        default="",
        description=(
            "Lab device whose Kubernetes Node is tainted. Defaults to a node "
            "that holds a local volume for a business PersistentVolumeClaim."
        ),
    )
    taint_key: str = Field(default=TAINT_KEY, description="Taint key to add.")
    taint_value: str = Field(default=TAINT_VALUE, description="Taint value to add.")


class NodeNoExecuteTaint(K8sProblemBase):
    """A node carries a NoExecute taint that the workload does not tolerate.

    Pods on that node are evicted. A Pod whose PersistentVolume is pinned to
    the node stays Pending; other nodes remain Ready. See taints and
    tolerations in the Kubernetes documentation:
    https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/
    """

    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name = "k8s_node_noexecute_taint"
    description = "A Kubernetes node has a NoExecute taint."
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s", "k8s_storage"]
    Params = NodeNoExecuteTaintParams
    RECORDED_ATTRS = (*K8sProblemBase.RECORDED_ATTRS, "k8s_node", "taint")

    @classmethod
    def benchmark_inject_params(cls, ctx):
        control = k8s_control_node(ctx)
        storage = _declared_storage_nodes(ctx.net_env)
        if not storage:
            raise ValueError(
                "k8s_node_noexecute_taint requires a declared storage node."
            )
        return {
            "control_node": control,
            "node_name": choice(ctx.rng, storage, storage[0]),
        }

    @classmethod
    def benchmark_node_targets(cls, ctx, targets):
        storage = set(_declared_storage_nodes(ctx.net_env))
        if storage:
            return [target for target in targets if target in storage]
        return []

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        node_name = inject.get("node_name") or ""
        storage = _declared_storage_nodes(ctx.net_env)
        if storage and node_name and node_name not in storage:
            raise ValueError(
                "k8s_node_noexecute_taint node_name must be a node with a local "
                f"volume; {node_name!r} is not in {storage}"
            )

    def __init__(self, scenario_name: str | None = None, **kwargs: Any) -> None:
        super().__init__(scenario_name, **kwargs)
        self.k8s_node: str | None = None
        self.taint: str | None = None

    def _target_device(self, params: NodeNoExecuteTaintParams) -> str:
        if params.node_name:
            return params.node_name
        live = _local_volume_nodes(self.runtime.lab_api, self.control_node(params))
        declared = _declared_storage_nodes(self.net_env)
        nodes = live or declared
        if not nodes:
            raise ValueError(
                "k8s_node_noexecute_taint needs a node whose local volume backs "
                "a business PersistentVolumeClaim."
            )
        return self.runtime.lab_api.k8s_device_for_node(
            self.control_node(params), nodes[0], devices=self.cluster_nodes()
        )

    def _taint_token(self, params: NodeNoExecuteTaintParams) -> str:
        return f"{params.taint_key}={params.taint_value}:{TAINT_EFFECT}"

    def _node_taints(self, params: NodeNoExecuteTaintParams) -> list[dict[str, Any]]:
        k8s = self.runtime.lab_api
        control = self.control_node(params)
        node = self.k8s_node or k8s.k8s_node_for_device(
            control, self._target_device(params), devices=self.cluster_nodes()
        )
        item = k8s.kubectl_json(control, f"get node {node}")
        return list((item.get("spec") or {}).get("taints") or [])

    def _taint_present(self, params: NodeNoExecuteTaintParams) -> bool:
        return any(
            taint.get("key") == params.taint_key
            and taint.get("value") == params.taint_value
            and taint.get("effect") == TAINT_EFFECT
            for taint in self._node_taints(params)
        )

    def root_cause_resources(self, params: NodeNoExecuteTaintParams):
        node = (params.node_name or "").strip()
        if not node:
            raise UnresolvedRootCauseError(
                "k8s_node_noexecute_taint needs node_name for a unique resource."
            )
        return [node_resource(node)]

    def inject_fault(self, params: NodeNoExecuteTaintParams) -> None:
        k8s = self.runtime.lab_api
        control = self.control_node(params)
        device = self._target_device(params)
        node = k8s.k8s_node_for_device(control, device, devices=self.cluster_nodes())
        self.k8s_node = node
        self.taint = self._taint_token(params)
        volumes = _local_volume_nodes(k8s, control)
        if node not in volumes:
            raise ValueError(
                f"{node} has no local volume bound to a business claim; "
                f"tainting it would reschedule the workload. Volumes on {volumes}."
            )
        k8s.kubectl(control, f"taint nodes {node} {self.taint}")
        self.record_k8s_object("Node", node)
        logger.info("Added taint %s on node %s.", self.taint, node)

    def verify_fault(self, params: NodeNoExecuteTaintParams) -> dict:
        def check():
            taints = self._node_taints(params)
            present = any(
                taint.get("key") == params.taint_key
                and taint.get("value") == params.taint_value
                and taint.get("effect") == TAINT_EFFECT
                for taint in taints
            )
            return present, {
                "node": self.k8s_node,
                "taint": self.taint,
                "taints": taints,
            }

        return self.poll_verify(check)

    def recover_fault(self, params: NodeNoExecuteTaintParams) -> dict:
        from nika.validation.symptom.k8s_probes import target_port_baseline

        k8s = self.runtime.lab_api
        control = self.control_node(params)
        node = self.k8s_node
        if node:
            k8s.kubectl(
                control,
                f"taint nodes {node} {params.taint_key}={params.taint_value}:"
                f"{TAINT_EFFECT}-",
                check=False,
            )

        def check():
            healthy, details = target_port_baseline(self, params)
            restored = node is not None and not self._taint_present(params)
            return restored and healthy, {"node": node, "taint": self.taint, **details}

        return self.poll_verify(check, timeout=900)


def _unserved_port(container: dict) -> int:
    """Return the first port above the container's own that it does not declare."""
    declared = {
        int(item["containerPort"])
        for item in container.get("ports") or ()
        if item.get("containerPort")
    }
    for handler in ("httpGet", "tcpSocket", "grpc"):
        port = ((container.get("readinessProbe") or {}).get(handler) or {}).get("port")
        if isinstance(port, int):
            declared.add(port)
    port = min(declared, default=8080) % 65535 + 1
    while port in declared:
        port = port % 65535 + 1
    return port


class ReadinessProbeMisconfigParams(DeploymentReplicasZeroParams):
    """Select the Deployment whose readiness endpoint is misconfigured."""


class ReadinessProbeMisconfig(K8sProblemBase):
    """A readiness probe addresses a port the application does not serve.

    An application that moves to another port, or a probe copied from another
    service, leaves the probe on a port nothing listens on.

    Kubelet keeps the containers running but marks their Pods not Ready, so
    Service EndpointSlices exclude them from ready backends. See Kubernetes:
    https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/
    https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/
    """

    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    root_cause_name = "k8s_readiness_probe_misconfig"
    description = "A Deployment readiness probe points at an unserved endpoint."
    symptom_probe = "custom"
    symptom_class = "unreachable"
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s"]
    COMPATIBLE_COLUMNS = {"k8s_lab", "llmd_lab"}
    Params = ReadinessProbeMisconfigParams
    BENCHMARK_TARGETS = "canonical"
    RECORDED_ATTRS = (*K8sProblemBase.RECORDED_ATTRS, "deployment_ref", "service_ref")

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"control_node": k8s_control_node(ctx)}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        if inject.get("namespace") and not inject.get("deployment_name"):
            raise ValueError("namespace requires deployment_name.")

    def __init__(self, scenario_name: str | None = None, **kwargs: Any) -> None:
        super().__init__(scenario_name, **kwargs)
        self.deployment_ref: str | None = None
        self.service_ref: str | None = None
        self.probe_port: int | None = None
        self._original: dict | None = None

    def _targets(self, params):
        return _deployment_targets(self, params)

    def root_cause_resources(self, params):
        return [node_resource(self.control_node(params))]

    def inject_fault(self, params):
        import copy

        k8s = self.runtime.lab_api
        control = self.control_node(params)
        namespace, name, service_ns, service = self._targets(params)
        original = _deployment_object(k8s, control, name, namespace)
        if not original["spec"].get("replicas"):
            raise ValueError("Deployment must have running replicas.")
        self._original = original
        self.deployment_ref = f"{namespace}/{name}"
        self.service_ref = f"{service_ns}/{service}"
        self.k8s_namespace = namespace
        self.record_k8s_object("deployment", name, namespace=namespace)
        container = original["spec"]["template"]["spec"]["containers"][0]
        probe = copy.deepcopy(container.get("readinessProbe") or {})
        port = _unserved_port(container)
        self.probe_port = port
        handler = next(
            (h for h in ("httpGet", "tcpSocket", "grpc") if h in probe), None
        )
        if handler:
            probe[handler]["port"] = port
        else:
            probe = {
                "httpGet": {"path": "/healthz", "port": port},
                "periodSeconds": 5,
                "failureThreshold": 1,
            }
        # Drain the previous ReplicaSet so rolling-update availability cannot
        # retain healthy old Pods alongside the unready replacement Pods.
        k8s.kubectl(control, f"scale deploy {name} -n {namespace} --replicas=0")
        selector = original["spec"]["selector"]["matchLabels"]
        labels = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
        drained = self.poll_verify(
            lambda: (
                not k8s.k8s_pods(control, namespace=namespace, selector=labels),
                {"deployment": self.deployment_ref},
            ),
            timeout=180,
        )
        if not drained["verified"]:
            raise RuntimeError(f"Deployment did not drain: {drained}")

        def edit(spec):
            spec["template"]["spec"]["containers"][0]["readinessProbe"] = copy.deepcopy(
                probe
            )

        edit_spec(k8s, control, original, edit)

    def verify_fault(self, params):
        def check():
            if not self.deployment_ref:
                return False, {"error": "No injected Deployment."}
            namespace, name = self.deployment_ref.split("/", 1)
            live = _deployment_object(
                self.runtime.lab_api, self.control_node(params), name, namespace
            )
            probe = (
                live["spec"]["template"]["spec"]["containers"][0].get("readinessProbe")
                or {}
            )
            present = any(
                probe.get(handler, {}).get("port") == self.probe_port
                for handler in ("httpGet", "tcpSocket", "grpc")
            )
            return present and live["spec"].get("replicas", 0) > 0, {
                "deployment": self.deployment_ref,
                "readiness_probe": probe,
            }

        return self.poll_verify(check)

    def recover_fault(self, params):
        if not self._original:
            return {"verified": False, "details": {"error": "No original Deployment."}}
        k8s = self.runtime.lab_api
        control = self.control_node(params)
        restore_spec(k8s, control, self._original)

        def check():
            meta = self._original["metadata"]
            live = _deployment_object(k8s, control, meta["name"], meta["namespace"])
            wanted = self._original["spec"]["replicas"]
            namespace, service = self.service_ref.split("/", 1)
            endpoints = k8s.k8s_service_endpoint_addresses(
                control, service, namespace=namespace
            )
            ok = _deployment_restored(live, self._original) and len(endpoints) >= wanted
            return ok, {
                "deployment": self.deployment_ref,
                "wanted_replicas": wanted,
                "status": live.get("status") or {},
                "ready_endpoints": len(endpoints),
            }

        return self.poll_verify(check, timeout=900)
