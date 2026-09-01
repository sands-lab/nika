"""Neutral topology model for lab runtimes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class NodeRole(StrEnum):
    HOST = "host"
    ROUTER = "router"
    SWITCH = "switch"
    CONTROLLER = "controller"
    SERVICE = "service"
    INFRASTRUCTURE = "infrastructure"


@dataclass(frozen=True)
class NodeIdentity:
    """Backend-neutral semantic identity declared by a scenario."""

    role: NodeRole
    capabilities: tuple[str, ...] = ()
    service_type: str | None = None
    reachability_target: bool = False

    def __post_init__(self) -> None:
        if self.role is NodeRole.SERVICE and not self.service_type:
            raise ValueError("Service node identity requires service_type")
        if self.role is not NodeRole.SERVICE and self.service_type is not None:
            raise ValueError("Only service node identities can set service_type")

    def to_dict(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "capabilities": list(self.capabilities),
            "service_type": self.service_type,
            "reachability_target": self.reachability_target,
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> NodeIdentity:
        return cls(
            role=NodeRole(str(value["role"])),
            capabilities=tuple(str(item) for item in value.get("capabilities", [])),
            service_type=(
                str(value["service_type"])
                if value.get("service_type") is not None
                else None
            ),
            reachability_target=bool(value.get("reachability_target", False)),
        )


@dataclass(frozen=True)
class MachineInventory:
    """Semantic identities for every machine in a lab."""

    nodes: dict[str, NodeIdentity]

    def validate(self, machine_names: set[str]) -> None:
        declared_names = set(self.nodes)
        if machine_names == declared_names:
            return
        missing = sorted(machine_names - declared_names)
        extra = sorted(declared_names - machine_names)
        raise ValueError(
            f"Machine identities do not match lab machines; missing={missing}, extra={extra}"
        )

    def names_for_role(self, role: NodeRole) -> list[str]:
        return sorted(
            name for name, identity in self.nodes.items() if identity.role is role
        )

    def names_for_capability(self, capability: str) -> list[str]:
        return sorted(
            name
            for name, identity in self.nodes.items()
            if capability in identity.capabilities
        )

    def services(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for name, identity in self.nodes.items():
            if identity.role is not NodeRole.SERVICE:
                continue
            assert identity.service_type is not None
            grouped.setdefault(identity.service_type, []).append(name)
        return {
            service_type: sorted(names)
            for service_type, names in sorted(grouped.items())
        }

    def reachability_targets(self) -> list[str]:
        return sorted(
            name
            for name, identity in self.nodes.items()
            if identity.reachability_target
        )

    def to_dict(self) -> dict[str, dict[str, object]]:
        return {
            name: identity.to_dict() for name, identity in sorted(self.nodes.items())
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> MachineInventory:
        malformed = sorted(
            name for name, identity in value.items() if not isinstance(identity, dict)
        )
        if malformed:
            raise ValueError(f"Malformed machine identities: {malformed}")
        return cls(
            nodes={
                name: NodeIdentity.from_dict(identity)
                for name, identity in value.items()
            }
        )


@dataclass
class NodeSpec:
    name: str
    image: str
    kind: str = "linux"
    binds: list[str] = field(default_factory=list)
    exec_cmds: list[str] = field(default_factory=list)


@dataclass
class LinkSpec:
    endpoints: tuple[str, str]


@dataclass
class LabSpec:
    name: str
    nodes: list[NodeSpec] = field(default_factory=list)
    links: list[LinkSpec] = field(default_factory=list)
