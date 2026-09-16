"""Mock-agent pipeline smoke (Kathara simple_bgp)."""

from __future__ import annotations

import pytest

from tests.nika.workflows.integration.pipeline_case import PipelineCaseBase
from tests.support.prerequisites import docker_available

pytestmark = [
    pytest.mark.ci_smoke,
    pytest.mark.skipif(not docker_available(), reason="Docker not available"),
]


class KatharaPipelineCiSmoke(PipelineCaseBase):
    """Same flow as integration pipeline; selected for dual-arch CI."""

    SCENARIO = "simple_bgp"
    BACKEND = "kathara"
    PROBLEM = "link_down"
    INJECT_PARAMS = {"host_name": "pc1", "intf_name": "eth0"}
    EXPECTED_NODES = frozenset({"pc1", "pc2", "router1", "router2"})
    EXEC_PROBE_HOST = "pc1"
    SUBMIT_FAULTY_DEVICES = ["pc1"]
    IMAGE_SUBSTRING = "nika"
    RUN_TRAFFIC = True
