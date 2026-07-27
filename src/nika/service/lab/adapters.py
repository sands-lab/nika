"""Adapters that expose LabRuntime to backend-neutral lab service APIs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nika.service.lab.frr_api import FRRAPIMixin
from nika.service.lab.intf_api import IntfAPIMixin
from nika.service.lab.k8s_api import K8sAPIMixin
from nika.service.lab.nft_api import NFTableMixin
from nika.service.lab.semantic_mixin import SemanticOpsMixin
from nika.service.lab.tc_api import TCMixin

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime


class LabRuntimeExecAdapter:
    """Bridge ``LabRuntime.exec`` to the ``exec_cmd`` protocol."""

    def __init__(self, runtime: LabRuntime) -> None:
        self._runtime = runtime

    @property
    def backend(self) -> str:
        return self._runtime.backend

    @property
    def lab_name(self) -> str:
        return self._runtime.lab_name

    def exec_cmd(self, host_name: str, command: str, timeout: float = 10) -> str:
        return self._runtime.exec(host_name, command, timeout=timeout)

    def list_nodes(self) -> list[str]:
        return self._runtime.list_nodes()

    def get_container(self, node: str):
        return self._runtime.get_container(node)


class LabRuntimeLabAPI(
    LabRuntimeExecAdapter,
    SemanticOpsMixin,
    TCMixin,
    IntfAPIMixin,
    NFTableMixin,
    FRRAPIMixin,
    K8sAPIMixin,
):
    """Shared semantic and traffic-control API backed by ``LabRuntime``."""

    def uses_srl_router(self, device_name: str) -> bool:
        return False


def lab_api_for_runtime(runtime: LabRuntime) -> LabRuntimeLabAPI:
    """Return the appropriate lab API adapter for ``runtime``'s backend."""
    if runtime.backend == "containerlab":
        from nika.service.containerlab.adapters import LabRuntimeContainerlabAPI

        return LabRuntimeContainerlabAPI(runtime)
    return LabRuntimeLabAPI(runtime)


def node_status_from_container(container) -> str:
    container.reload()
    return str(container.status)
