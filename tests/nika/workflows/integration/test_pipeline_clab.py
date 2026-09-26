from __future__ import annotations

import pytest
from typing import ClassVar
from tests.nika.workflows.integration import pipeline_case
from tests.support.integration_pipeline import _min3clos_prerequisites

MIN3CLOS_NODES = frozenset({"leaf1", "leaf2", "spine", "client1", "client2"})


@pytest.mark.skipif(
    not _min3clos_prerequisites(), reason="containerlab, gnmic, or Docker not available"
)
class ClabPipelineIntegrationTest(pipeline_case.PipelineCaseBase):
    SCENARIO = "min3clos"
    BACKEND = "containerlab"
    ENV_RUN_ARGS: ClassVar[list[str]] = []
    PROBLEM = "link_down"
    INJECT_PARAMS = {"host_name": "leaf1", "intf_name": "e1-1"}
    EXPECTED_NODES = MIN3CLOS_NODES
    EXEC_PROBE_HOST = "client1"
    SUBMIT_FAULTY_DEVICES = ["leaf1"]
    IMAGE_SUBSTRING = None
    DIAGNOSIS_MCP_SERVERS = ["kathara_base_mcp_server", "containerlab_srl_mcp_server"]

    async def _extra_diagnosis_mcp_checks(self, tools: dict) -> dict[str, str]:
        bgp_as = await tools["srl_exec_cli"].ainvoke(
            {
                "device_name": "leaf1",
                "command": "show network-instance default protocols bgp summary",
            }
        )
        routes = await tools["srl_exec_cli"].ainvoke(
            {
                "device_name": "leaf1",
                "command": "show network-instance default route-table ipv4-unicast summary",
            }
        )
        return {
            "srl_bgp_summary": str(bgp_as),
            "srl_routes": str(routes),
        }
