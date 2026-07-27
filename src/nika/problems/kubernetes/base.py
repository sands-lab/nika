import time
from collections.abc import Callable
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from nika.problems.problem_base import (
    ProblemBase,
    ProblemGroundTruth,
    build_verify_result,
)
from nika.service.lab.k8s_api import K8sCommandError

KUBECTL_TIMEOUT_SEC: float = 60
K8S_VERIFY_TIMEOUT_SEC: float = 120
K8S_VERIFY_POLL_SEC: float = 5


class K8sParams(BaseModel):
    """Parameters shared by every Kubernetes fault."""

    control_node: str | None = Field(
        default=None,
        description="k3s control-plane device name; auto-resolved from the lab when omitted.",
    )


class K8sWorkloadParams(K8sParams):
    """Parameters for faults that target a namespaced workload."""

    namespace: str = Field(description="Target Kubernetes namespace.")
    workload: str = Field(description="Target workload (Deployment) name.")


def control_node_from_net_env(net_env: Any) -> str | None:
    explicit = getattr(net_env, "kubernetes_control_node", None)
    if explicit:
        return str(explicit)

    k8s_nodes = list(getattr(net_env, "kubernetes_nodes", None) or [])
    lab = getattr(net_env, "lab", None)
    if lab is not None:
        for name in k8s_nodes:
            machine = lab.machines.get(name)
            if machine is None:
                continue
            args = str(machine.meta.get("args") or "").strip()
            if args.startswith("server"):
                return name

    for keyword in ("controller", "control", "master", "server"):
        for name in k8s_nodes:
            if keyword in name:
                return name

    return k8s_nodes[0] if k8s_nodes else None


class K8sProblemBase(ProblemBase):
    supported_backends: ClassVar[tuple[str, ...]] = ("kathara",)
    required_capabilities: ClassVar[tuple[str, ...]] = ("exec", "k8s")
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s"]

    #: Which lab devices are reported as faulty: the control plane (for faults
    #: that change API-server desired state), the affected cluster nodes, or both.
    FAULTY_DEVICE_POLICY: ClassVar[str] = "control_plane"

    def __init__(self, scenario_name: str | None = None, **kwargs: Any) -> None:
        super().__init__(scenario_name, **kwargs)
        self.k8s_control_node: str | None = None
        self.k8s_objects: list[str] = []
        self.k8s_namespace: str | None = None
        self.k8s_workload: str | None = None

    def control_node(self, params: BaseModel | None = None) -> str | None:
        explicit = getattr(params, "control_node", None) if params is not None else None
        if explicit:
            self.k8s_control_node = str(explicit)
            return self.k8s_control_node
        if self.k8s_control_node is None:
            resolved = control_node_from_net_env(self.net_env)
            if not resolved:
                raise ValueError(
                    f"{type(self).__name__} requires a Kubernetes scenario: "
                    f"{self.scenario_name!r} exposes no k3s nodes. Check the problem "
                    "TAGS against the scenario TAGS."
                )
            self.k8s_control_node = resolved
        return self.k8s_control_node

    def cluster_nodes(self) -> list[str]:
        return list(getattr(self.net_env, "kubernetes_nodes", None) or [])

    def worker_devices(self, params: BaseModel | None = None) -> list[str]:
        control = self.control_node(params)
        workers = sorted(node for node in self.cluster_nodes() if node != control)
        if workers:
            return workers
        return [control] if control else []

    def resolve_params(
            self, params: BaseModel | dict[str, Any] | None = None, **overrides: Any
    ) -> BaseModel | None:
        resolved = super().resolve_params(params, **overrides)
        if isinstance(resolved, K8sParams) and not resolved.control_node:
            resolved = resolved.model_copy(update={"control_node": self.control_node()})
        return resolved

    def record_k8s_object(self, kind: str, name: str, *, namespace: str | None = None) -> None:
        ref = f"{namespace}/{kind}/{name}" if namespace else f"{kind}/{name}"
        if ref not in self.k8s_objects:
            self.k8s_objects.append(ref)

    def faulty_devices_for(
            self, params: BaseModel | None = None, *, affected: list[str | None] | None = None
    ) -> list[str | None]:
        control = [self.control_node(params)]
        nodes = [device for device in (affected or []) if device]
        match self.FAULTY_DEVICE_POLICY:
            case "affected_nodes":
                return nodes or control
            case "both":
                return control + [device for device in nodes if device not in control]
            case _:
                return control

    def get_ground_truth(self) -> ProblemGroundTruth:
        ground_truth = super().get_ground_truth()
        if self.k8s_objects:
            objects = ", ".join(self.k8s_objects)
            detail = (ground_truth.detailed_cause or "").strip()
            ground_truth.detailed_cause = (
                f"{detail} Affected Kubernetes object(s): {objects}.".strip()
            )
        return ground_truth

    def poll_verify(
            self,
            check: Callable[[], tuple[bool, dict[str, Any]]],
            *,
            timeout: float | None = None,
    ) -> dict:
        deadline = time.monotonic() + (timeout or K8S_VERIFY_TIMEOUT_SEC)
        started = time.monotonic()
        attempts = 0
        while True:
            attempts += 1
            try:
                verified, details = check()
            except K8sCommandError as exc:
                verified, details = False, {"error": str(exc)}
            if verified or time.monotonic() >= deadline:
                break
            time.sleep(K8S_VERIFY_POLL_SEC)

        return build_verify_result(
            root_cause_name=self.root_cause_name,
            faulty_devices=self.faulty_devices,
            verified=verified,
            details={
                **details,
                "k8s_objects": list(self.k8s_objects),
                "control_node": self.k8s_control_node,
                "poll_attempts": attempts,
                "waited_sec": round(time.monotonic() - started, 1),
            },
        )

    def get_task_description(self) -> str:
        base = super().get_task_description()
        nodes = ", ".join(self.cluster_nodes())
        control = self.control_node()
        return (
            f"{base}\n\n"
            f"Kubernetes: this lab runs a k3s cluster on devices [{nodes}]. "
            f"`kubectl` works only on {control} (the k3s server); worker devices ship the "
            "binary but have no kubeconfig. Report faulty devices using lab device names, "
            "not Kubernetes object names."
        )
