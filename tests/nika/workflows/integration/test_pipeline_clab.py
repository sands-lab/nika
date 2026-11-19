from __future__ import annotations

import pytest
from nika.net_env.clos.design import build_clos_fabric_model
from typing import ClassVar
from tests.nika.workflows.integration import pipeline_case
from tests.support.prerequisites import containerlab_prerequisites

pytestmark = pytest.mark.e2e


_model = build_clos_fabric_model("s")
CLOS_NODES = frozenset(
    [*_model.leaves, *_model.spines, *(e.name for e in _model.endpoints)]
)


@pytest.mark.skipif(
    not containerlab_prerequisites(),
    reason="containerlab, gnmic, or Docker not available",
)
class ClabPipelineIntegrationTest(pipeline_case.PipelineCaseBase):
    SCENARIO = "dc_clos"
    BACKEND = "containerlab"
    ENV_RUN_ARGS: ClassVar[list[str]] = ["-s", "s", "--backend", "containerlab"]
    PROBLEM = "link_down"
    INJECT_PARAMS = {"host_name": "leaf_1", "intf_name": "e1-1"}
    EXPECTED_NODES = CLOS_NODES
    EXEC_PROBE_HOST = "client_1_1"
    SUBMIT_FAULTY_DEVICES = ["leaf_1"]
    IMAGE_SUBSTRING = None
    DIAGNOSIS_MCP_SERVERS = ["kathara_base_mcp_server", "containerlab_srl_mcp_server"]

    async def _extra_diagnosis_mcp_checks(self, tools: dict) -> dict[str, str]:
        bgp_as = await tools["srl_exec_cli"].ainvoke(
            {
                "device_name": "leaf_1",
                "command": "show network-instance default protocols bgp summary",
            }
        )
        routes = await tools["srl_exec_cli"].ainvoke(
            {
                "device_name": "leaf_1",
                "command": "show network-instance default route-table ipv4-unicast summary",
            }
        )
        return {
            "srl_bgp_summary": str(bgp_as),
            "srl_routes": str(routes),
        }
