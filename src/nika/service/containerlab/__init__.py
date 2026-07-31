from nika.service.containerlab.adapters import LabRuntimeContainerlabAPI
from nika.service.containerlab.base_api import ContainerlabBaseAPI
from nika.service.containerlab.srl_api import SRLAPIMixin
from nika.service.lab.host_api import create_host_api


class ContainerlabSRLAPI(ContainerlabBaseAPI, SRLAPIMixin):
    """Containerlab API with SR Linux router operations."""

    def exec_cmd(self, host_name: str, command: str, timeout: float = 10) -> str:
        # runtime.exec already wraps commands in /bin/sh -c; avoid ShellResolver double-wrap.
        return self.runtime.exec(host_name, command, timeout=timeout)


__all__ = [
    "ContainerlabBaseAPI",
    "ContainerlabSRLAPI",
    "LabRuntimeContainerlabAPI",
    "SRLAPIMixin",
    "create_host_api",
]
