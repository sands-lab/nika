"""Docker e2e: Optus-inspired BGP maximum-prefix exceeded (Kathara + FRR)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from nika.net_env.isp.bgp import compile_bgp_plan
from nika.net_env.isp.igp import IspConfig, compile_isp_plan
from nika.net_env.isp.inject_targets import isp_inject_params
from nika.problems.registry import get_problem_class
from nika.service.kathara import KatharaFRRAPI
from nika.utils.session_store import SessionStore
from nika.workflows.eval.session import run_eval_metrics
from nika.workflows.session.close import close_session
from tests.agent._assertions import (
    _extract_tool_names,
    assert_phase_messages,
    assert_submission_fields,
)
from tests.nika.workflows.integration import pipeline_case
from tests.support.integration_base import IntegrationTestCase, OrderedPipelineTestCase
from tests.support.integration_pipeline import (
    deepseek_api_key_available,
    load_test_env,
)
from tests.support.prerequisites import docker_available

load_test_env()

PROBLEM = "bgp_max_prefix_exceeded"
ENV_ARGS = ["--topo", "abilene", "--igp", "ospf", "--bgp-mode", "ebgp"]
AGENT_MAX_STEPS = 40
_BGP_TOOLS = (
    "frr_get_routing_state",
    "frr_exec",
    "frr_get_bgp_conf",
    "frr_get_rpki_status",
    "traceroute",
    "get_reachability",
    "run_pingmesh_snapshot",
)


def _inject_params() -> dict[str, str]:
    isp_plan = compile_isp_plan(IspConfig(topology="abilene", igp="ospf"))
    bgp = compile_bgp_plan(isp_plan, "ebgp")
    assert bgp is not None
    return isp_inject_params(PROBLEM, isp_plan.inventory, bgp.inventory)


def _abilene_nodes() -> frozenset[str]:
    isp_plan = compile_isp_plan(IspConfig(topology="abilene", igp="ospf"))
    routers = sorted(str(n["device"]) for n in isp_plan.inventory["nodes"])
    # Edge stubs are always attached for ISP Kathara labs.
    stubs = [f"pc_{r}" for r in routers]
    return frozenset(routers + stubs)


def _diagnosis_tool_names(messages: list[dict]) -> list[str]:
    from agent.protocols import DIAGNOSIS

    names: list[str] = []
    for entry in messages:
        if entry["phase"] != DIAGNOSIS:
            continue
        names.extend(n for n in _extract_tool_names(entry) if n)
    return names


def _assert_bgp_tool_use(messages: list[dict]) -> None:
    diag_tools = _diagnosis_tool_names(messages)
    assert diag_tools, "diagnosis phase must call MCP tools"
    hits = [n for n in diag_tools if any(t in n for t in _BGP_TOOLS)]
    assert hits, (
        f"diagnosis must call BGP/reachability telemetry tools; "
        f"saw {sorted(set(diag_tools))}"
    )
    assert any("frr_get_routing_state" in n for n in diag_tools), (
        f"diagnosis must call frr_get_routing_state; saw {sorted(set(diag_tools))}"
    )


def _assert_fault_type_submitted(session_dir: Path, params: dict[str, str]) -> None:
    assert_submission_fields(session_dir)
    submission = json.loads((session_dir / "submission.json").read_text())
    causes = submission.get("root_causes") or []
    assert causes, "submission root_causes empty"
    expected_nodes = {
        f"node/{params['receiver_name']}",
        f"node/{params['peer_name']}",
    }
    matched_type = False
    matched_node = False
    for item in causes:
        resource_id = item.get("resource_id") or (item.get("resource") or {}).get(
            "id", ""
        )
        fault_type = item.get("fault_type") or ""
        if fault_type == PROBLEM:
            matched_type = True
        if resource_id in expected_nodes and fault_type == PROBLEM:
            matched_node = True
    assert matched_type, (
        f"expected fault_type {PROBLEM}; got {json.dumps(causes, ensure_ascii=False)}"
    )
    assert matched_node, (
        f"expected at least one of {sorted(expected_nodes)} with {PROBLEM}; "
        f"got {json.dumps(causes, ensure_ascii=False)}"
    )


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class TestBGPMaxPrefixExceededE2E(IntegrationTestCase):
    """Healthy → inject → verify → recover on Abilene eBGP."""

    def test_healthy_inject_recover_cycle(self) -> None:
        params = _inject_params()
        assert params["receiver_name"]
        assert params["peer_name"]
        assert params["neighbor_ip"]

        session_id = self._start_env("isp", ENV_ARGS)
        try:
            self._assert_session_ready(session_id, "isp")
            row = self._session_row(session_id)
            lab_name = row["lab_name"]
            time.sleep(35)

            frr = KatharaFRRAPI(lab_name=lab_name)
            receiver = params["receiver_name"]
            peer = params["peer_name"]
            neighbor_ip = params["neighbor_ip"]

            healthy = frr.frr_get_routing_state(receiver, neighbor=neighbor_ip)
            assert "bgp state = established" in healthy.lower()
            assert f"neighbor {neighbor_ip} maximum-prefix" not in healthy.lower()

            self._inject_failure(PROBLEM, params, session_id=session_id)
            self._assert_failure_injected(PROBLEM, session_id=session_id)

            time.sleep(5)
            broken = frr.frr_get_routing_state(receiver, neighbor=neighbor_ip)
            assert "bgp state = established" not in broken.lower()
            evidence_blob = broken.lower()
            assert any(
                token in evidence_blob
                for token in (
                    "maximum-prefix",
                    "maximum prefix",
                    "prefix limit",
                    "prefix count",
                    "maxpfx",
                    "idle",
                    "active",
                    "connect",
                )
            ), broken[:1200]

            cls = get_problem_class(PROBLEM)
            assert cls is not None
            problem = self._problem(cls, session_id=session_id)
            parsed = problem.parse_params(params)
            recovered = problem.recover_fault(parsed)
            assert recovered["ok"], recovered

            time.sleep(10)
            restored = frr.frr_get_routing_state(receiver, neighbor=neighbor_ip)
            assert "bgp state = established" in restored.lower()
            # Flood prefixes should be gone after recover.
            rib = frr.frr_get_routing_state(receiver, prefix="198.19.0.0/24")
            assert "198.19.0.0" not in rib or "Network not in table" in rib
            _ = peer  # peer labeled in GT; session restore checked on receiver
        finally:
            self._close_session(session_id)


class _BGPMaxPrefixAgentPipelineBase(OrderedPipelineTestCase):
    """Real byo.mcp_agent on injected max-prefix lab; check tools + submit."""

    llm_provider: str = ""
    model: str = ""
    session_id: str | None = None
    session_dir: Path | None = None
    env_destroyed: bool = False
    _params: dict[str, str] | None = None

    def test_step_01_start_env(self) -> None:
        type(self).session_id = self._start_env("isp", ENV_ARGS)
        self._assert_session_ready(self.session_id, "isp")
        time.sleep(35)

    def test_step_02_inject_failure(self) -> None:
        assert self.session_id is not None
        params = _inject_params()
        type(self)._params = params
        self._inject_failure(PROBLEM, params, session_id=self.session_id)
        self._assert_failure_injected(PROBLEM, session_id=self.session_id)
        row = SessionStore().get_session(self.session_id)
        type(self).session_dir = Path(row["session_dir"])

    def test_step_03_run_agent(self) -> None:
        assert self.session_id is not None
        self._run_agent(
            agent_type="byo.mcp_agent",
            llm_provider=self.llm_provider,
            model=self.model,
            max_steps=AGENT_MAX_STEPS,
            session_id=self.session_id,
        )
        row = SessionStore().get_session(self.session_id)
        assert row.get("agent_type") == "byo.mcp_agent"

    def test_step_04_check_messages_and_tools(self) -> None:
        assert self.session_dir is not None
        messages = self._load_jsonl("messages.jsonl")
        assert_phase_messages(messages, require_submission_tools=True)
        _assert_bgp_tool_use(messages)

    def test_step_05_check_submit_and_eval(self) -> None:
        assert self.session_id is not None
        assert self.session_dir is not None
        assert self._params is not None
        _assert_fault_type_submitted(self.session_dir, self._params)
        close_session(session_id=self.session_id)
        type(self).env_destroyed = True
        run_eval_metrics(session_id=self.session_id)
        metrics = self._load_json("eval_metrics.json")
        assert metrics.get("detection_score", 0) >= 1.0
        assert metrics.get("tool_calls", 0) >= 1


@pytest.mark.skipif(
    not (docker_available() and deepseek_api_key_available()),
    reason="Docker and DEEPSEEK_API_KEY required for max-prefix agent e2e",
)
class TestBGPMaxPrefixExceededAgentDeepseek(_BGPMaxPrefixAgentPipelineBase):
    llm_provider = "deepseek"
    model = "deepseek-chat"


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class KatharaBGPMaxPrefixPipelineIntegrationTest(pipeline_case.PipelineCaseBase):
    """env → inject → MCP → mock agent → close → metrics for max-prefix."""

    SCENARIO = "isp"
    BACKEND = "kathara"
    ENV_RUN_ARGS = ENV_ARGS
    PROBLEM = PROBLEM
    INJECT_PARAMS = _inject_params()
    EXPECTED_NODES = _abilene_nodes()
    EXEC_PROBE_HOST = INJECT_PARAMS["receiver_name"]
    SUBMIT_FAULTY_DEVICES = [
        INJECT_PARAMS["receiver_name"],
        INJECT_PARAMS["peer_name"],
    ]
    ROOT_CAUSE_CATEGORY = "routing_control_plane"
    IMAGE_SUBSTRING = None
    DIAGNOSIS_MCP_SERVERS = [
        "kathara_base_mcp_server",
        "kathara_frr_mcp_server",
        "pingmesh_mcp_server",
    ]

    async def _extra_diagnosis_mcp_checks(self, tools: dict) -> dict[str, str]:
        assert "frr_get_routing_state" in tools
        out = await tools["frr_get_routing_state"].ainvoke(
            {
                "device": self.INJECT_PARAMS["receiver_name"],
                "neighbor": self.INJECT_PARAMS["neighbor_ip"],
            }
        )
        return {"frr_get_routing_state": str(out)}
