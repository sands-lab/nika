"""LabRuntime protocol for Kathara and Containerlab backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from docker.models.containers import Container

from nika.runtime.ops_mixin import ExecSemanticOpsMixin


class RuntimeCapabilityError(RuntimeError):
    """Raised when a runtime backend cannot provide a requested operation."""


class LabRuntime(ExecSemanticOpsMixin, ABC):
    """Backend-neutral lab lifecycle, exec, and semantic observation APIs."""

    DEFAULT_CAPABILITIES = frozenset(
        {
            "exec",
            "inspect",
            "node_status",
            "interface",
            "ip",
            "route",
            "dns",
            "service",
            "tc",
            "nft",
            "iptables",
            "process",
            "pidfile",
            "file",
            "frr",
            "traffic",
            "k8s",
        }
    )

    @property
    def backend(self) -> str:
        return type(self).__name__

    @property
    def capabilities(self) -> frozenset[str]:
        return self.DEFAULT_CAPABILITIES

    @property
    def lab_api(self):
        """Backend-neutral lab operations API (host, tc, nft, frr, …)."""
        from nika.service.lab.adapters import lab_api_for_runtime

        return lab_api_for_runtime(self)

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    def require_capabilities(self, *capabilities: str) -> None:
        missing = [
            capability
            for capability in capabilities
            if not self.has_capability(capability)
        ]
        if not missing:
            return
        missing_text = ", ".join(missing)
        supported = ", ".join(sorted(self.capabilities)) or "none"
        raise RuntimeCapabilityError(
            f"Runtime backend {self.backend!r} for lab {self.lab_name!r} does not support "
            f"required capability/capabilities: {missing_text}. Supported capabilities: {supported}."
        )

    @property
    @abstractmethod
    def lab_name(self) -> str:
        """Logical lab name used by sessions and workflows."""

    @abstractmethod
    def deploy(self) -> None:
        """Deploy the lab if it is not already running."""

    @abstractmethod
    def destroy(self) -> None:
        """Tear down the lab."""

    @abstractmethod
    def exists(self) -> bool:
        """Return True when the lab has at least one running node."""

    @abstractmethod
    def inspect(self) -> list[dict[str, Any]]:
        """Return container rows aligned with ``list_lab_containers`` shape."""

    @abstractmethod
    def list_nodes(self) -> list[str]:
        """Return logical node names in the lab."""

    @abstractmethod
    def exec(self, node: str, cmd: str, *, timeout: float = 10.0) -> str:
        """Run a command inside ``node`` and return stdout/stderr text."""

    @abstractmethod
    def get_container(self, node: str) -> Container:
        """Return the Docker container for logical ``node``."""

    @abstractmethod
    def pause(self, node: str) -> None:
        """Pause the container backing ``node``."""

    @abstractmethod
    def unpause(self, node: str) -> None:
        """Unpause the container backing ``node``."""

    def get_connected_devices(self, node: str) -> list[str]:
        """Return neighbor node names connected to ``node``; override when topology is available."""
        return []
