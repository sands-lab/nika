from __future__ import annotations
from tests.nika.workflows.integration import pipeline_case


class KatharaPipelineIntegrationTest(pipeline_case.PipelineCaseBase):
    SCENARIO = "simple_bgp"
    BACKEND = "kathara"
    PROBLEM = "link_down"
    INJECT_PARAMS = {"host_name": "pc1", "intf_name": "eth0"}
    EXPECTED_NODES = frozenset({"pc1", "pc2", "router1", "router2"})
    EXEC_PROBE_HOST = "pc1"
    SUBMIT_FAULTY_DEVICES = ["pc1"]
    IMAGE_SUBSTRING = "nika"
    RUN_TRAFFIC = True
