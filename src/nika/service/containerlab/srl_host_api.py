"""Containerlab host API with SR Linux router operations."""

from __future__ import annotations

from nika.service.containerlab.base_api import ContainerlabBaseAPI
from nika.service.containerlab.srl_api import SRLAPIMixin


class ContainerlabSRLAPI(ContainerlabBaseAPI, SRLAPIMixin):
    """Containerlab API with SR Linux router operations."""

    def exec_cmd(self, host_name: str, command: str, timeout: float = 10) -> str:
        # runtime.exec already wraps commands in /bin/sh -c; avoid ShellResolver double-wrap.
        return self.runtime.exec(host_name, command, timeout=timeout)
