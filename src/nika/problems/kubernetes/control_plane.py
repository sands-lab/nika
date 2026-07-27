from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from nika.problems.kubernetes.base import K8sParams, K8sProblemBase
from nika.problems.kubernetes.node_filter import DropSpec, NodeFilter, NodeFilterError
from nika.problems.problem_base import RootCauseCategory
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
            "API server address to block. Defaults to the control-plane node's "
            "InternalIP, which is what the k3s agent connects to."
        ),
    )


class WorkerApiServerPartition(K8sProblemBase):
    root_cause_category: RootCauseCategory = RootCauseCategory.MISCONFIGURATION
    root_cause_name: str = "k8s_worker_apiserver_partition"
    symptom_desc = (
        "One Kubernetes worker node reports NotReady and stops receiving new pods, "
        "and `kubectl exec` / `kubectl logs` time out for the pods it hosts, while "
        "those pods keep serving traffic and the node itself is still reachable over "
        "the network."
    )
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s", "k8s_control_plane"]
    FAULTY_DEVICE_POLICY: ClassVar[str] = "affected_nodes"

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

    def _apiserver_address(self, params: WorkerApiServerPartitionParams, k8s: Any) -> str:
        if params.apiserver_address:
            return params.apiserver_address

        control = self.control_node(params)
        control_node_name = k8s.k8s_node_for_device(
            control, control, devices=self.cluster_nodes()
        )
        for entry in k8s.k8s_nodes(control):
            if entry.get("name") == control_node_name and entry.get("internal_ip"):
                return str(entry["internal_ip"])

        address = self.runtime.get_host_ip(control)
        if not address:
            raise ValueError(
                f"Cannot resolve the API server address for {control!r}: the node has "
                "no InternalIP and no address on its first interface. Pass "
                "--set apiserver_address=<ip>."
            )
        return address

    def _drop_specs(self, params: WorkerApiServerPartitionParams, k8s: Any) -> list[DropSpec]:
        return [
            DropSpec(
                self._apiserver_address(params, k8s),
                protocol="tcp",
                port=params.apiserver_port,
            )
        ]

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
        self.set_faulty_devices(self.faulty_devices_for(params, affected=[device]))
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
        node_name = k8s.k8s_node_for_device(control, device, devices=self.cluster_nodes())
        specs = self._drop_specs(params, k8s)
        node_filter = NodeFilter(self.runtime, device)
        apiserver_address = specs[0].destination

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

            apiserver_reachable = node_filter.tcp_reachable(
                apiserver_address, params.apiserver_port
            )
            # Ping still works: this is a port-scoped partition, not a dead node.
            control_pingable = self.runtime.ping_ok(device, apiserver_address, count=2)

            details: dict[str, Any] = {
                "target_device": device,
                "k8s_node": node_name,
                "apiserver_address": apiserver_address,
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
