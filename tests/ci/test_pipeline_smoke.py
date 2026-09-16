"""Mock-agent pipeline smoke on a published Kathara scenario (dc_clos)."""

from __future__ import annotations

from typing import ClassVar

import pytest

from tests.nika.workflows.integration.pipeline_case import PipelineCaseBase
from tests.support.prerequisites import docker_available

pytestmark = [
    pytest.mark.ci_smoke,
    pytest.mark.skipif(not docker_available(), reason="Docker not available"),
]

_DC_CLOS_S_NODES = frozenset(
    {
        "super_spine_router_0",
        "spine_router_0_0",
        "spine_router_0_1",
        "leaf_router_0_0",
        "leaf_router_0_1",
        "dns_pod0",
        "webserver0_pod0",
        "client_0",
    }
)


class KatharaPipelineCiSmoke(PipelineCaseBase):
    """Same user workflow as integration pipeline; dual-arch CI selection."""

    SCENARIO = "dc_clos"
    BACKEND = "kathara"
    ENV_RUN_ARGS: ClassVar[list[str]] = ["-s", "s"]
    PROBLEM = "link_down"
    INJECT_PARAMS = {"host_name": "client_0", "intf_name": "eth0"}
    EXPECTED_NODES = _DC_CLOS_S_NODES
    EXEC_PROBE_HOST = "client_0"
    SUBMIT_FAULTY_DEVICES = ["client_0"]
    IMAGE_SUBSTRING = "nika"
    RUN_TRAFFIC = True
