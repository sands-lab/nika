"""Mock-agent pipeline smoke on a published Kathara scenario (dc_clos)."""

from __future__ import annotations

from typing import ClassVar

import pytest

from nika.net_env.clos.design import build_clos_fabric_model

from tests.nika.workflows.integration.pipeline_case import PipelineCaseBase
from tests.support.prerequisites import docker_available

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.ci_smoke,
    pytest.mark.skipif(not docker_available(), reason="Docker not available"),
]


_design = build_clos_fabric_model("s")
_DC_CLOS_S_NODES = frozenset(
    _design.spines + _design.leaves + [e.name for e in _design.endpoints]
)


class KatharaPipelineCiSmoke(PipelineCaseBase):
    """Same user workflow as integration pipeline; dual-arch CI selection."""

    SCENARIO = "dc_clos"
    BACKEND = "kathara"
    ENV_RUN_ARGS: ClassVar[list[str]] = ["-s", "s"]
    PROBLEM = "link_down"
    INJECT_PARAMS = {"host_name": "client_1_1", "intf_name": "eth0"}
    EXPECTED_NODES = _DC_CLOS_S_NODES
    EXEC_PROBE_HOST = "client_1_1"
    SUBMIT_FAULTY_DEVICES = ["client_1_1"]
    IMAGE_SUBSTRING = "nika"
    RUN_TRAFFIC = True
