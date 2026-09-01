from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from nika.problems.rca import UnresolvedRootCauseError, node_resource
from nika.problems.support.kubernetes.base import K8sParams, K8sProblemBase
from nika.problems.support.kubernetes.node_filter import (
    DropSpec,
    NodeFilter,
    NodeFilterError,
)
from nika.problems.base import (
    FailureDomain,
)
from nika.utils.logger import system_logger

logger = system_logger

APISERVER_PORT = 6443
APISERVER_SERVICE = "kubernetes"
APISERVER_NAMESPACE = "default"
APISERVER_SERVICE_PORT = 443

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
    root_cause_name: str = "k8s_worker_apiserver_partition"
    description = "Worker is partitioned from the Kubernetes API server."
    symptom_desc = (
        "One Kubernetes worker node reports NotReady and stops receiving new pods, "
        "and `kubectl exec` / `kubectl logs` time out for the pods it hosts, while "
        "those pods keep serving traffic and the node itself is still reachable over "
        "the network."
    )
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s", "k8s_control_plane"]

    Params = WorkerApiServerPartitionParams

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
