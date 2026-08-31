"""Session-scoped Kubernetes client for the host-side MCP server."""

from __future__ import annotations

import json
import os
import shlex
import threading
import time
from pathlib import Path
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream

from nika.mcp.session_context import get_session_meta, require_session_id

DEFAULT_KUBECONFIG = "/etc/rancher/k3s/k3s.yaml"


def _as_json(payload: Any) -> str:
    return json.dumps(payload, default=str, indent=2)


def resolve_kubeconfig_path(meta: dict[str, Any] | None = None) -> Path:
    """Return the host kubeconfig path for the bound session."""
    session_meta = meta if meta is not None else get_session_meta()
    metadata = session_meta.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    raw = metadata.get("kubeconfig_path")
    if raw:
        path = Path(str(raw))
        if path.is_file():
            return path

    workdir = session_meta.get("runtime_workdir") or (
        session_meta.get("scenario_params") or {}
    ).get("runtime_workdir")
    if workdir:
        path = Path(str(workdir)) / "kubeconfig.yaml"
        if path.is_file():
            return path

    lab_name = session_meta.get("lab_name") or (
        session_meta.get("scenario_params") or {}
    ).get("lab_name")
    if lab_name:
        from nika.config import RUNTIME_DIR

        path = Path(RUNTIME_DIR) / "kathara" / str(lab_name) / "kubeconfig.yaml"
        if path.is_file():
            return path

    raise FileNotFoundError(
        f"No kubeconfig.yaml for session {session_meta.get('session_id')!r}. "
        "Ensure post_deploy wrote the host kubeconfig for this k8s lab."
    )


class K8sClient:
    """Thin wrapper around the official Kubernetes Python client."""

    def __init__(
        self,
        *,
        kubeconfig: str | Path | None = None,
        apiserver: str | None = None,
    ) -> None:
        self.kubeconfig = str(
            kubeconfig
            if kubeconfig is not None
            else os.environ.get("KUBECONFIG", DEFAULT_KUBECONFIG)
        )
        if apiserver is not None:
            self.apiserver = apiserver
        else:
            try:
                from nika.run_config.loader import get_run_config

                self.apiserver = get_run_config().nika.k8s.apiserver
            except Exception:  # noqa: BLE001
                self.apiserver = None
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        configuration = client.Configuration()
        config.load_kube_config(
            config_file=self.kubeconfig,
            client_configuration=configuration,
        )
        if self.apiserver:
            configuration.host = self.apiserver
        configuration.verify_ssl = False
        self._api_client = client.ApiClient(configuration)
        self._core = client.CoreV1Api(self._api_client)
        self._apps = client.AppsV1Api(self._api_client)
        self._networking = client.NetworkingV1Api(self._api_client)
        self._discovery = client.DiscoveryV1Api(self._api_client)
        self._loaded = True

    @property
    def core(self) -> client.CoreV1Api:
        self._ensure_loaded()
        return self._core

    @property
    def apps(self) -> client.AppsV1Api:
        self._ensure_loaded()
        return self._apps

    @property
    def networking(self) -> client.NetworkingV1Api:
        self._ensure_loaded()
        return self._networking

    @property
    def discovery(self) -> client.DiscoveryV1Api:
        self._ensure_loaded()
        return self._discovery

    def list_nodes(self) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        for item in self.core.list_node().items:
            status = item.status or client.V1NodeStatus()
            conditions = {
                (c.type or ""): (c.status or "") for c in (status.conditions or [])
            }
            internal_ip = next(
                (
                    addr.address
                    for addr in (status.addresses or [])
                    if addr.type == "InternalIP"
                ),
                None,
            )
            nodes.append(
                {
                    "name": item.metadata.name if item.metadata else "",
                    "ready": conditions.get("Ready") == "True",
                    "schedulable": not bool(
                        item.spec.unschedulable if item.spec else False
                    ),
                    "internal_ip": internal_ip,
                    "conditions": conditions,
                }
            )
        return nodes

    def get_node(self, name: str) -> dict[str, Any]:
        item = self.core.read_node(name)
        return self._api_client.sanitize_for_serialization(item)

    def list_pods(
        self,
        *,
        namespace: str | None = None,
        selector: str | None = None,
        field_selector: str | None = None,
        all_namespaces: bool = False,
    ) -> list[dict[str, Any]]:
        if all_namespaces:
            payload = self.core.list_pod_for_all_namespaces(
                label_selector=selector or None,
                field_selector=field_selector or None,
            )
        else:
            ns = namespace or "default"
            payload = self.core.list_namespaced_pod(
                ns,
                label_selector=selector or None,
                field_selector=field_selector or None,
            )
        pods: list[dict[str, Any]] = []
        for item in payload.items:
            meta = item.metadata or client.V1ObjectMeta()
            status = item.status or client.V1PodStatus()
            spec = item.spec or client.V1PodSpec(containers=[])
            container_statuses = status.container_statuses or []
            pods.append(
                {
                    "name": meta.name or "",
                    "namespace": meta.namespace or "",
                    "phase": status.phase or "",
                    "ready": bool(container_statuses)
                    and all(cs.ready for cs in container_statuses),
                    "node": spec.node_name or "",
                    "pod_ip": status.pod_ip or "",
                    "restarts": sum(
                        int(cs.restart_count or 0) for cs in container_statuses
                    ),
                    "images": [c.image or "" for c in (spec.containers or [])],
                }
            )
        return pods

    def get_pod(self, name: str, *, namespace: str = "default") -> dict[str, Any]:
        item = self.core.read_namespaced_pod(name, namespace)
        return self._api_client.sanitize_for_serialization(item)

    def get_logs(
        self,
        name: str,
        *,
        namespace: str = "default",
        container: str | None = None,
        tail_lines: int = 200,
        since_seconds: int | None = None,
        timeout_seconds: int = 30,
    ) -> str:
        try:
            return self.core.read_namespaced_pod_log(
                name,
                namespace,
                container=container,
                tail_lines=tail_lines,
                since_seconds=since_seconds,
                _request_timeout=timeout_seconds,
            )
        except ApiException as exc:
            return _as_json(
                {
                    "error": "log_fetch_failed",
                    "status": exc.status,
                    "reason": exc.reason,
                    "details": exc.body,
                }
            )

    def list_events(
        self,
        *,
        namespace: str | None = None,
        field_selector: str | None = None,
        all_namespaces: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if all_namespaces:
            payload = self.core.list_event_for_all_namespaces(
                field_selector=field_selector or None,
                limit=limit,
            )
        else:
            ns = namespace or "default"
            payload = self.core.list_namespaced_event(
                ns,
                field_selector=field_selector or None,
                limit=limit,
            )
        events: list[dict[str, Any]] = []
        for item in payload.items:
            meta = item.metadata or client.V1ObjectMeta()
            involved = item.involved_object
            events.append(
                {
                    "namespace": meta.namespace or "",
                    "name": meta.name or "",
                    "type": item.type or "",
                    "reason": item.reason or "",
                    "message": item.message or "",
                    "count": item.count,
                    "last_timestamp": str(item.last_timestamp or item.event_time or ""),
                    "involved_object": {
                        "kind": involved.kind if involved else "",
                        "name": involved.name if involved else "",
                        "namespace": involved.namespace if involved else "",
                    },
                }
            )
        return events

    def list_services(
        self,
        *,
        namespace: str | None = None,
        all_namespaces: bool = False,
    ) -> list[dict[str, Any]]:
        if all_namespaces:
            payload = self.core.list_service_for_all_namespaces()
        else:
            payload = self.core.list_namespaced_service(namespace or "default")
        services: list[dict[str, Any]] = []
        for item in payload.items:
            meta = item.metadata or client.V1ObjectMeta()
            spec = item.spec or client.V1ServiceSpec()
            ports = []
            for entry in spec.ports or []:
                ports.append(
                    {
                        "name": entry.name or "",
                        "port": entry.port,
                        "target_port": entry.target_port,
                        "protocol": entry.protocol or "TCP",
                    }
                )
            services.append(
                {
                    "name": meta.name or "",
                    "namespace": meta.namespace or "",
                    "type": spec.type or "",
                    "cluster_ip": spec.cluster_ip or "",
                    "selector": dict(spec.selector or {}),
                    "ports": ports,
                }
            )
        return services

    def get_endpoints(
        self, service: str, *, namespace: str = "default"
    ) -> dict[str, Any]:
        slices = self.discovery.list_namespaced_endpoint_slice(
            namespace,
            label_selector=f"kubernetes.io/service-name={service}",
        )
        addresses: list[str] = []
        ports: list[dict[str, Any]] = []
        for slice_item in slices.items:
            for endpoint in slice_item.endpoints or []:
                conditions = endpoint.conditions
                ready = True if conditions is None else bool(conditions.ready)
                if not ready:
                    continue
                for address in endpoint.addresses or []:
                    if address not in addresses:
                        addresses.append(address)
            for port in slice_item.ports or []:
                ports.append(
                    {
                        "name": port.name or "",
                        "port": port.port,
                        "protocol": port.protocol or "TCP",
                    }
                )
        try:
            svc = self.core.read_namespaced_service(service, namespace)
            cluster_ip = (svc.spec.cluster_ip if svc.spec else "") or ""
        except ApiException:
            cluster_ip = ""
        return {
            "service": service,
            "namespace": namespace,
            "cluster_ip": "" if cluster_ip in ("", "None") else cluster_ip,
            "addresses": addresses,
            "ports": ports,
        }

    def get_network_policies(
        self,
        *,
        namespace: str | None = None,
        name: str | None = None,
        all_namespaces: bool = False,
    ) -> Any:
        if name:
            ns = namespace or "default"
            item = self.networking.read_namespaced_network_policy(name, ns)
            return self._api_client.sanitize_for_serialization(item)
        if all_namespaces:
            payload = self.networking.list_network_policy_for_all_namespaces()
        else:
            payload = self.networking.list_namespaced_network_policy(
                namespace or "default"
            )
        return [
            {
                "name": (item.metadata.name if item.metadata else ""),
                "namespace": (item.metadata.namespace if item.metadata else ""),
                "pod_selector": self._api_client.sanitize_for_serialization(
                    item.spec.pod_selector if item.spec else {}
                ),
                "policy_types": list(item.spec.policy_types or []) if item.spec else [],
            }
            for item in payload.items
        ]

    def exec_in_pod(
        self,
        name: str,
        command: list[str] | str,
        *,
        namespace: str = "default",
        container: str | None = None,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        if isinstance(command, str):
            cmd = ["/bin/sh", "-c", command]
        else:
            cmd = list(command)
        try:
            resp = stream(
                self.core.connect_get_namespaced_pod_exec,
                name,
                namespace,
                command=cmd,
                container=container,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
            )
            deadline = time.monotonic() + timeout_seconds
            stdout_chunks: list[str] = []
            stderr_chunks: list[str] = []
            while resp.is_open():
                if time.monotonic() > deadline:
                    resp.close()
                    return {
                        "ok": False,
                        "error": "timeout",
                        "stdout": "".join(stdout_chunks),
                        "stderr": "".join(stderr_chunks),
                        "command": cmd,
                    }
                resp.update(timeout=1)
                if resp.peek_stdout():
                    stdout_chunks.append(resp.read_stdout())
                if resp.peek_stderr():
                    stderr_chunks.append(resp.read_stderr())
            return {
                "ok": True,
                "stdout": "".join(stdout_chunks),
                "stderr": "".join(stderr_chunks),
                "command": cmd,
            }
        except ApiException as exc:
            return {
                "ok": False,
                "error": "exec_failed",
                "status": exc.status,
                "reason": exc.reason,
                "details": exc.body,
                "command": cmd,
            }

    def dns_query(
        self,
        name: str,
        query: str,
        *,
        namespace: str = "default",
        server: str | None = None,
        timeout_seconds: int = 20,
    ) -> dict[str, Any]:
        quoted = shlex.quote(query)
        if server:
            cmd = f"nslookup {quoted} {shlex.quote(server)} || dig +short @{shlex.quote(server)} {quoted} || getent hosts {quoted}"
        else:
            cmd = f"nslookup {quoted} || dig +short {quoted} || getent hosts {quoted}"
        result = self.exec_in_pod(
            name, cmd, namespace=namespace, timeout_seconds=timeout_seconds
        )
        result["query"] = query
        result["server"] = server
        return result

    def check_connectivity(
        self,
        name: str,
        target: str,
        *,
        namespace: str = "default",
        port: int | None = None,
        protocol: str = "tcp",
        timeout_seconds: int = 15,
    ) -> dict[str, Any]:
        proto = protocol.lower()
        if proto == "http":
            url = target if "://" in target else f"http://{target}"
            cmd = (
                f"wget -q -O - --timeout={timeout_seconds} {shlex.quote(url)} "
                f"|| curl -sS -m {timeout_seconds} {shlex.quote(url)}"
            )
        elif proto == "tcp":
            if port is None:
                raise ValueError("port is required for tcp connectivity checks")
            host = shlex.quote(target)
            cmd = (
                f"nc -z -w {timeout_seconds} {host} {int(port)} "
                f"|| (echo >/dev/tcp/{target}/{int(port)})"
            )
        else:
            raise ValueError(f"unsupported protocol: {protocol!r}")
        result = self.exec_in_pod(
            name, cmd, namespace=namespace, timeout_seconds=timeout_seconds + 5
        )
        result["target"] = target
        result["port"] = port
        result["protocol"] = proto
        return result


_CLIENTS: dict[str, K8sClient] = {}
_CLIENTS_LOCK = threading.Lock()


def get_client() -> K8sClient:
    """Return a K8sClient for the currently bound MCP session (cached by session_id)."""
    session_id = require_session_id()
    with _CLIENTS_LOCK:
        cached = _CLIENTS.get(session_id)
        if cached is not None:
            return cached
        kubeconfig = resolve_kubeconfig_path()
        client_obj = K8sClient(kubeconfig=kubeconfig)
        _CLIENTS[session_id] = client_obj
        return client_obj


def reset_client(session_id: str | None = None) -> None:
    """Drop cached clients (all, or one session)."""
    with _CLIENTS_LOCK:
        if session_id is None:
            _CLIENTS.clear()
            return
        _CLIENTS.pop(session_id, None)
