"""Closed-catalog root-cause objects: resource + fault type.

Resources are lab-enumerable: node, interface (RFC 8343 if:name), link
(undirected termination-point set), and k8s objects. Joint scoring uses the
RFC 8632 alarm instance key ``(resource.id, fault_type)``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator


class UnresolvedRootCauseError(ValueError):
    """Raised when a fault resource cannot be derived without guessing."""


class ResourceKind(StrEnum):
    NODE = "node"
    INTERFACE = "interface"
    LINK = "link"
    K8S = "k8s"


def _require(value: str | None, *, field: str, kind: ResourceKind) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{kind} resource requires {field}")
    return text


class FaultResource(BaseModel):
    """Fine-grained faulted object. Equality is by canonical ``id``."""

    model_config = ConfigDict(extra="ignore")

    kind: ResourceKind
    node: str | None = None
    name: str | None = None
    namespace: str | None = None
    k8s_kind: str | None = None

    @model_validator(mode="after")
    def _validate_kind_fields(self) -> FaultResource:
        kind = self.kind
        if kind == ResourceKind.NODE:
            _require(self.node, field="node", kind=kind)
        elif kind == ResourceKind.INTERFACE:
            _require(self.node, field="node", kind=kind)
            _require(self.name, field="name", kind=kind)
        elif kind == ResourceKind.LINK:
            _require(self.name, field="name", kind=kind)
        elif kind == ResourceKind.K8S:
            _require(self.k8s_kind, field="k8s_kind", kind=kind)
            _require(self.name, field="name", kind=kind)
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        kind = self.kind
        if kind == ResourceKind.NODE:
            return f"node/{self.node}"
        if kind == ResourceKind.INTERFACE:
            return f"interface/{self.node}/{self.name}"
        if kind == ResourceKind.LINK:
            return f"link/{self.name}"
        ns = self.namespace or ""
        if ns:
            return f"k8s/{self.k8s_kind}/{ns}/{self.name}"
        return f"k8s/{self.k8s_kind}/{self.name}"


def resource_from_id(resource_id: str) -> FaultResource:
    """Parse a catalog id into a ``FaultResource``."""
    text = (resource_id or "").strip()
    parts = text.split("/")
    if not parts or not parts[0]:
        raise ValueError(f"Invalid resource_id {resource_id!r}.")
    kind = parts[0]
    if kind == "node" and len(parts) == 2 and parts[1]:
        return node_resource(parts[1])
    if kind == "interface" and len(parts) == 3 and parts[1] and parts[2]:
        return interface_resource(parts[1], parts[2])
    if kind == "link" and text.startswith("link/"):
        body = text[len("link/") :].strip()
        if body:
            return link_resource(body)
    if kind == "k8s" and len(parts) == 4 and parts[1] and parts[2] and parts[3]:
        return k8s_resource(parts[1], parts[3], namespace=parts[2])
    if kind == "k8s" and len(parts) == 3 and parts[1] and parts[2]:
        return k8s_resource(parts[1], parts[2])
    raise ValueError(
        f"Invalid resource_id {resource_id!r}. "
        "Expected node/{{name}}, interface/{{node}}/{{ifname}}, "
        "link/{{sorted-tps}}, or k8s/{{kind}}/{{namespace}}/{{name}}."
    )


class RootCause(BaseModel):
    """One independent fault source: catalog resource + failure ID."""

    model_config = ConfigDict(extra="ignore")

    resource: FaultResource | None = None
    resource_id: str | None = None
    fault_type: str = Field(min_length=1)

    @model_validator(mode="after")
    def _sync_resource(self) -> RootCause:
        if self.resource is not None:
            self.resource_id = self.resource.id
            return self
        if self.resource_id:
            self.resource = resource_from_id(self.resource_id)
            self.resource_id = self.resource.id
            return self
        raise ValueError("root cause requires resource or resource_id")

    def pair_key(self) -> tuple[str, str]:
        assert self.resource is not None
        return (self.resource.id, self.fault_type)


def node_resource(name: str) -> FaultResource:
    return FaultResource(kind=ResourceKind.NODE, node=name)


def interface_resource(node: str, name: str) -> FaultResource:
    return FaultResource(kind=ResourceKind.INTERFACE, node=node, name=name)


def link_resource(name: str) -> FaultResource:
    return FaultResource(kind=ResourceKind.LINK, name=name)


def k8s_resource(
    kind: str, name: str, *, namespace: str | None = None
) -> FaultResource:
    return FaultResource(
        kind=ResourceKind.K8S, k8s_kind=kind, name=name, namespace=namespace
    )


class ProblemGroundTruth(BaseModel):
    """Detection + structured RCA ground truth.

    ``failure_domain`` is metadata; core RCA is ``root_causes``.
    """

    is_anomaly: bool = Field(
        description="Whether an anomaly is present in the network."
    )
    root_causes: list[RootCause] = Field(default_factory=list)
    failure_domain: str = Field(
        default="", description="Network subsystem in which the failure occurs."
    )


def canonical_root_causes(items: list[RootCause] | list[dict[str, Any]]) -> list[dict]:
    parsed: list[RootCause] = []
    for item in items:
        if isinstance(item, RootCause):
            parsed.append(item)
        else:
            parsed.append(RootCause.model_validate(item))
    parsed.sort(key=lambda rc: rc.pair_key())
    dumped: list[dict[str, Any]] = []
    for rc in parsed:
        assert rc.resource is not None
        dumped.append(
            {
                "resource": rc.resource.model_dump(
                    mode="json", exclude_none=True, exclude={"id"}
                ),
                "fault_type": rc.fault_type,
            }
        )
    return dumped


def healthy_ground_truth() -> ProblemGroundTruth:
    return ProblemGroundTruth(
        is_anomaly=False,
        root_causes=[],
        failure_domain="",
    )
