"""Shared Kubernetes object-edit and traffic-backend helpers."""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from typing import Any

from nika.service.lab.k8s_api import K8sCommandError

DEFAULT_PROBE_NAMESPACE = "default"

LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


def _object_ref(obj: dict[str, Any]) -> str:
    meta = obj["metadata"]
    scope = f" -n {meta['namespace']}" if meta.get("namespace") else ""
    return f"{obj['kind'].lower()} {meta['name']}{scope}"


def edit_spec(
    k8s: Any,
    control: str,
    obj: dict[str, Any],
    edit: Callable[[dict], None],
    *,
    annotations: dict[str, str | None] | None = None,
) -> None:
    """Edit the spec and selected annotations in the live and last-applied objects.

    The annotation then shows the edited manifest an operator would have
    applied, not the server-filled live object.
    """
    spec = json.loads(json.dumps(obj["spec"]))
    edit(spec)
    _patch_spec(k8s, control, obj, obj["spec"], spec)
    if annotations:
        _patch_annotations(k8s, control, obj, annotations)
    applied = (obj["metadata"].get("annotations") or {}).get(LAST_APPLIED)
    if applied:
        manifest = json.loads(applied)
        edit(manifest.setdefault("spec", {}))
        if annotations:
            target = manifest.setdefault("metadata", {}).setdefault("annotations", {})
            for key, value in annotations.items():
                if value is None:
                    target.pop(key, None)
                else:
                    target[key] = value
        _annotate(k8s, control, obj, json.dumps(manifest, separators=(",", ":")))


def restore_spec(
    k8s: Any,
    control: str,
    obj: dict[str, Any],
    *,
    annotation_keys: tuple[str, ...] = (),
) -> None:
    """Restore the spec, selected annotations, and last-applied manifest."""
    live = k8s.kubectl_json(control, f"get {_object_ref(obj)}")
    _patch_spec(k8s, control, obj, live["spec"], obj["spec"])
    if annotation_keys:
        original = obj["metadata"].get("annotations") or {}
        _patch_annotations(
            k8s, control, obj, {key: original.get(key) for key in annotation_keys}
        )
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


def _patch_annotations(k8s, control, obj, annotations: dict[str, str | None]) -> None:
    patch = {"metadata": {"annotations": annotations}}
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


def _resource_items(k8s: Any, control: str, resource: str) -> list[dict[str, Any]]:
    result = k8s.kubectl(control, f"get {resource} -A -o json", check=False)
    if not result.ok:
        if "doesn't have a resource type" in result.stderr:
            return []
        raise K8sCommandError(
            f"Cannot read {resource}: {result.stderr or result.stdout}"
        )
    payload = result.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise K8sCommandError(f"Invalid {resource} list: {payload!r}")
    return payload["items"]


def traffic_backends(k8s: Any, control: str) -> list[tuple[str, str, str]]:
    """``(namespace, service, url)`` for Services on user-visible HTTP paths.

    Prefers Ingress HTTP backends. When the cluster has none, uses each
    InferencePool endpoint-picker Service and the first Gateway address.
    """
    backends = [
        (namespace, service, f"http://{host}{path}")
        for namespace, service, host, path in ingress_backends(k8s, control)
    ]
    if backends:
        return backends
    ip = ""
    for gateway in _resource_items(k8s, control, "gateway"):
        for address in (gateway.get("status") or {}).get("addresses") or []:
            value = address.get("value") or ""
            if value:
                ip = value
                break
        if ip:
            break
    if not ip:
        return []
    url = f"http://{ip}/"
    pickers = []
    for pool in _resource_items(k8s, control, "inferencepool"):
        ref = (pool.get("spec") or {}).get("endpointPickerRef") or {}
        name = ref.get("name")
        if name and (ref.get("kind") or "Service") == "Service":
            pickers.append((pool["metadata"]["namespace"], name, url))
    return sorted(pickers)


def selected_service(
    k8s: Any, control: str, service_name: str, namespace: str
) -> tuple[str, str]:
    """Return ``(namespace, name)`` for an explicit Service or the first traffic backend."""
    if service_name:
        return namespace or DEFAULT_PROBE_NAMESPACE, service_name
    backends = [
        backend
        for backend in traffic_backends(k8s, control)
        if not namespace or backend[0] == namespace
    ]
    if not backends:
        raise ValueError("No Ingress or business-traffic Service to change.")
    return backends[0][0], backends[0][1]
