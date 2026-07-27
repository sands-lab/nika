"""Shared Kubernetes (k3s) control-plane API for Kathara scenarios."""

from __future__ import annotations

import json
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from nika.service.lab.protocols import SupportsExec

KUBECONFIG_PATH: str = "/etc/rancher/k3s/k3s.yaml"
DEFAULT_SERVICE_CIDR: str = "10.43.0.0/16"
DEFAULT_SERVICE_CIDR_PREFIX: int = 16
KUBECTL_TIMEOUT_SEC: float = 60
KUBECTL_EXEC_MARGIN_SEC: float = 15.0

_RC_MARK = "__NIKA_RC="
_ERR_MARK = "__NIKA_ERR="
_STDERR_FILE = "/tmp/.nika_k8s_stderr"
_TIMEOUT_SENTINEL = "[TIMEOUT]"


def _ns(namespace: str | None) -> str:
    return f" -n {shlex.quote(namespace)}" if namespace else ""


def _as_int(value: str, default: int = 0) -> int:
    text = (value or "").strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        return default


class K8sCommandError(RuntimeError):
    """Raised when a ``kubectl`` invocation fails or returns unusable output."""


class K8sTimeoutError(K8sCommandError):
    """Raised when a ``kubectl`` invocation produced no exit-code marker."""


@dataclass(frozen=True)
class KubectlResult:
    """Outcome of a single ``kubectl`` invocation."""

    node: str
    command: str
    stdout: str
    stderr: str
    returncode: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def json(self) -> Any:
        try:
            return json.loads(self.stdout)
        except json.JSONDecodeError as exc:
            raise K8sCommandError(
                f"kubectl {self.command!r} on {self.node!r} returned non-JSON output: "
                f"{self.stdout[:200]!r}"
            ) from exc


class K8sAPIMixin:
    def kubectl(
            self: SupportsExec,
            node: str,
            args: str,
            *,
            timeout: float | None = None,
            check: bool = True,
            kubeconfig: str = KUBECONFIG_PATH,
    ) -> KubectlResult:
        timeout = KUBECTL_TIMEOUT_SEC if timeout is None else timeout
        base = f"kubectl --kubeconfig={shlex.quote(kubeconfig)} --request-timeout={int(timeout)}s"
        command = f"{base} {args} 2>{_STDERR_FILE}; printf '\\n{_RC_MARK}%s\\n{_ERR_MARK}\\n' $?; cat {_STDERR_FILE}"
        raw = self.exec_cmd(node, command, timeout=timeout + KUBECTL_EXEC_MARGIN_SEC)
        if raw.startswith(_TIMEOUT_SENTINEL) or _RC_MARK not in raw:
            raise K8sTimeoutError(
                f"kubectl {args!r} on {node!r} produced no exit-code marker "
                f"(timed out or shell unavailable): {raw[:200]!r}"
            )
        stdout, _, tail = raw.partition(_RC_MARK)
        rc_text, _, stderr = tail.partition(_ERR_MARK)
        result = KubectlResult(
            node=node,
            command=args,
            stdout=stdout.strip(),
            stderr=stderr.strip(),
            returncode=_as_int(rc_text, default=1),
        )
        if check and not result.ok:
            raise K8sCommandError(
                f"kubectl {args!r} on {node!r} failed with rc={result.returncode}: "
                f"{result.stderr or result.stdout}"
            )
        return result

    def kubectl_json(
            self: SupportsExec, node: str, args: str, *, timeout: float | None = None
    ) -> Any:
        return self.kubectl(node, f"{args} -o json", timeout=timeout).json()

    def kubectl_jsonpath(
            self: SupportsExec,
            node: str,
            target: str,
            jsonpath: str,
            *,
            namespace: str | None = None,
            timeout: float | None = None,
    ) -> str:
        args = f"get {target}{_ns(namespace)} -o jsonpath='{jsonpath}'"
        return self.kubectl(node, args, timeout=timeout).stdout

    def k8s_nodes(self: SupportsExec, node: str) -> list[dict[str, Any]]:
        payload = self.kubectl_json(node, "get nodes")
        nodes: list[dict[str, Any]] = []
        for item in payload.get("items", []):
            status = item.get("status", {})
            conditions = {
                cond.get("type"): cond.get("status")
                for cond in status.get("conditions", [])
            }
            internal_ip = next(
                (
                    addr.get("address")
                    for addr in status.get("addresses", [])
                    if addr.get("type") == "InternalIP"
                ),
                None,
            )
            nodes.append(
                {
                    "name": item.get("metadata", {}).get("name", ""),
                    "ready": conditions.get("Ready") == "True",
                    "schedulable": not item.get("spec", {}).get("unschedulable", False),
                    "internal_ip": internal_ip,
                }
            )
        return nodes

    def k8s_node_names(self: SupportsExec, node: str) -> list[str]:
        return [entry["name"] for entry in self.k8s_nodes(node) if entry["name"]]

    def k8s_get_object(
            self: SupportsExec,
            node: str,
            kind: str,
            name: str,
            *,
            namespace: str | None = None,
    ) -> dict[str, Any]:
        return self.kubectl_json(node, f"get {kind} {name}{_ns(namespace)}")

    def k8s_pods(
            self: SupportsExec,
            node: str,
            *,
            namespace: str | None = None,
            selector: str | None = None,
            field_selector: str | None = None,
            all_namespaces: bool = False,
    ) -> list[dict[str, Any]]:
        scope = " -A" if all_namespaces else _ns(namespace)
        args = f"get pods{scope}"
        if selector:
            args += f" -l {shlex.quote(selector)}"
        if field_selector:
            args += f" --field-selector {shlex.quote(field_selector)}"
        payload = self.kubectl_json(node, args)
        pods: list[dict[str, Any]] = []
        for item in payload.get("items", []):
            status = item.get("status", {})
            container_statuses = status.get("containerStatuses", []) or []
            containers = item.get("spec", {}).get("containers", []) or []
            pods.append(
                {
                    "name": item.get("metadata", {}).get("name", ""),
                    "namespace": item.get("metadata", {}).get("namespace", ""),
                    "phase": status.get("phase", ""),
                    "ready": bool(container_statuses)
                             and all(cs.get("ready") for cs in container_statuses),
                    "node": item.get("spec", {}).get("nodeName", ""),
                    "restarts": sum(
                        int(cs.get("restartCount", 0)) for cs in container_statuses
                    ),
                    "images": [c.get("image", "") for c in containers],
                }
            )
        return pods

    def k8s_service_endpoint_addresses(
            self: SupportsExec, node: str, service: str, *, namespace: str
    ) -> list[str]:
        args = (
            f"get endpointslice{_ns(namespace)} "
            f"-l {shlex.quote(f'kubernetes.io/service-name={service}')}"
        )
        payload = self.kubectl_json(node, args)
        addresses: list[str] = []
        for slice_item in payload.get("items", []):
            for endpoint in slice_item.get("endpoints", []) or []:
                conditions = endpoint.get("conditions", {}) or {}
                if not conditions.get("ready", True):
                    continue
                for address in endpoint.get("addresses", []) or []:
                    if address not in addresses:
                        addresses.append(address)
        return addresses

    def k8s_service_cluster_ip(
            self: SupportsExec, node: str, service: str, *, namespace: str
    ) -> str:
        cluster_ip = self.kubectl_jsonpath(
            node, f"service/{service}", "{.spec.clusterIP}", namespace=namespace
        ).strip()
        return "" if cluster_ip in ("", "None") else cluster_ip

    def k8s_service_ports(
            self: SupportsExec, node: str, service: str, *, namespace: str
    ) -> list[dict[str, Any]]:
        payload = self.k8s_get_object(node, "service", service, namespace=namespace)
        ports: list[dict[str, Any]] = []
        for entry in payload.get("spec", {}).get("ports", []) or []:
            ports.append(
                {
                    "name": entry.get("name", ""),
                    "port": entry.get("port"),
                    "target_port": entry.get("targetPort", entry.get("port")),
                    "protocol": entry.get("protocol", "TCP"),
                }
            )
        return ports

    def k8s_node_map(
            self: SupportsExec,
            node: str,
            *,
            devices: Iterable[str] | None = None,
    ) -> dict[str, str]:
        cached = getattr(self, "_k8s_node_map_cache", None)
        device_list = sorted(devices or [])
        if cached is not None and cached.get("devices") == device_list:
            return dict(cached["mapping"])

        cluster_nodes = self.k8s_nodes(node)
        by_name = {entry["name"]: entry for entry in cluster_nodes}
        by_ip = {
            entry["internal_ip"]: entry["name"]
            for entry in cluster_nodes
            if entry.get("internal_ip")
        }

        mapping: dict[str, str] = {}
        for device in device_list or list(by_name):
            if device in by_name:
                mapping[device] = device
                continue
            hostname = self.exec_cmd(device, "hostname").strip()
            if hostname in by_name:
                mapping[device] = hostname
                continue
            address = self.get_host_ip(device)
            if address and address in by_ip:
                mapping[device] = by_ip[address]

        self._k8s_node_map_cache = {"devices": device_list, "mapping": dict(mapping)}
        return mapping

    def k8s_node_for_device(
            self: SupportsExec,
            node: str,
            device: str,
            *,
            devices: Iterable[str] | None = None,
    ) -> str:
        mapping = self.k8s_node_map(node, devices=devices or [device])
        if device not in mapping:
            raise K8sCommandError(
                f"Device {device!r} does not map to any Kubernetes node "
                f"(known nodes: {sorted(self.k8s_node_names(node))})"
            )
        return mapping[device]

    def k8s_device_for_node(
            self: SupportsExec,
            node: str,
            k8s_node: str,
            *,
            devices: Iterable[str] | None = None,
    ) -> str:
        mapping = self.k8s_node_map(node, devices=devices)
        for device, name in mapping.items():
            if name == k8s_node:
                return device
        return k8s_node
