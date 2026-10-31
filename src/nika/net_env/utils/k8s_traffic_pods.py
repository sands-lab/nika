"""Run traffic programs on Kubernetes nodes as host-network pods.

k3s nodes have no Python, so a background traffic profile
(:mod:`nika.traffic.profiles`) cannot start its programs there with an exec.
Each program instead runs in a pod pinned to its node with ``hostNetwork``: it
uses the node's addresses and routes, as a program on the node would. The
image is one the lab already caches, so no registry access is needed.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Iterable

from nika.config import pkg_path
from nika.runtime.base import LabRuntime
from nika.traffic.profiles import POD_APPS_DIR
from nika.traffic.workload import WorkloadProcess

NAMESPACE = "traffic"
APPS_CONFIGMAP = "traffic-apps"
APPS_DIR = POD_APPS_DIR
READY_TIMEOUT_SEC = 180
# Pods must still run, without restarts, this long after they become ready.
SETTLE_SEC = 5


def _pod_name(process: WorkloadProcess) -> str:
    return re.sub(r"[^a-z0-9-]", "-", f"{process.command}-{process.node}".lower())


def _manifest(processes: list[WorkloadProcess], image: str) -> dict:
    apps = sorted({p.app for p in processes})
    items: list[dict] = [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}},
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": APPS_CONFIGMAP, "namespace": NAMESPACE},
            "data": {
                f"{app}.py": pkg_path("traffic", "apps", f"{app}.py").read_text(
                    encoding="utf-8"
                )
                for app in apps
            },
        },
    ]
    for process in processes:
        items.append(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": _pod_name(process),
                    "namespace": NAMESPACE,
                    "labels": {"app": process.command},
                },
                "spec": {
                    "nodeName": process.node,
                    "hostNetwork": True,
                    "dnsPolicy": "ClusterFirstWithHostNet",
                    "tolerations": [{"operator": "Exists"}],
                    "containers": [
                        {
                            "name": process.command,
                            "image": image,
                            "imagePullPolicy": "Never",
                            "command": [
                                "python3",
                                f"{APPS_DIR}/{process.app}.py",
                                *process.args,
                            ],
                            "volumeMounts": [{"name": "apps", "mountPath": APPS_DIR}],
                            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}},
                        }
                    ],
                    "volumes": [
                        {"name": "apps", "configMap": {"name": APPS_CONFIGMAP}}
                    ],
                },
            }
        )
    return {"apiVersion": "v1", "kind": "List", "items": items}


def start_pod_programs(
    runtime: LabRuntime,
    controller: str,
    processes: Iterable[WorkloadProcess],
    image: str,
) -> None:
    """Apply one pod per process from ``controller`` and wait until all run.

    Idempotent: re-applying the same pods changes nothing. Raises
    ``RuntimeError`` when a pod does not become ready or restarts.
    """
    processes = list(processes)
    if not processes:
        return
    encoded = base64.b64encode(
        json.dumps(_manifest(processes, image)).encode()
    ).decode()
    runtime.exec(
        controller,
        f"printf '%s' {encoded} | base64 -d | kubectl apply -f - >/dev/null",
        timeout=60,
    )
    names = [_pod_name(p) for p in processes]
    runtime.exec(
        controller,
        f"kubectl -n {NAMESPACE} wait --for=condition=Ready "
        f"--timeout={READY_TIMEOUT_SEC}s pod {' '.join(names)} >/dev/null 2>&1",
        timeout=READY_TIMEOUT_SEC + 30,
    )
    time.sleep(SETTLE_SEC)
    status = runtime.exec(
        controller,
        # A list query keeps ``.items`` even when only one pod runs.
        f"kubectl -n {NAMESPACE} get pods -o jsonpath="
        "'{range .items[*]}{.metadata.name} {.status.phase} "
        '{.status.containerStatuses[0].restartCount}{"\\n"}{end}\'',
        timeout=30,
    )
    running = {
        parts[0]
        for parts in (line.split() for line in status.splitlines())
        if len(parts) == 3 and parts[1] == "Running" and parts[2] == "0"
    }
    failed = sorted(set(names) - running)
    if failed:
        raise RuntimeError(f"traffic pods did not start: {', '.join(failed)}")


class K8sTrafficPods:
    """Run background traffic programs of k3s nodes as pods.

    A lab sets ``TRAFFIC_POD_IMAGE`` to a cached image with Python 3 and
    keeps its k3s nodes in ``kubernetes_nodes`` and its control plane on
    ``controller``.
    """

    TRAFFIC_POD_IMAGE: str
    kubernetes_nodes: list[str]

    def traffic_pod_nodes(self) -> frozenset[str]:
        return frozenset(self.kubernetes_nodes or ())

    def stop_traffic_programs(self) -> None:
        self._build_runtime().exec(
            "controller",
            f"kubectl delete namespace {NAMESPACE} --ignore-not-found --wait=true "
            "--timeout=120s >/dev/null 2>&1 || true",
            timeout=150,
        )

    def start_traffic_programs(self, workload) -> None:
        from nika.traffic.workload import BusinessWorkload, start_workload

        pod_nodes = self.traffic_pod_nodes()
        runtime = self._build_runtime()
        start_workload(
            runtime,
            BusinessWorkload(
                processes=tuple(
                    p for p in workload.processes if p.node not in pod_nodes
                )
            ),
        )
        start_pod_programs(
            runtime,
            "controller",
            [p for p in workload.processes if p.node in pod_nodes],
            self.TRAFFIC_POD_IMAGE,
        )
