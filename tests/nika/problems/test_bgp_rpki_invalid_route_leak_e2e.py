"""Docker e2e: Abilene eBGP RPKI-invalid route leak (Kathara + FRR + Routinator)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from nika.net_env.isp.bgp import compile_bgp_plan
from nika.net_env.isp.igp import IspConfig, compile_isp_plan
from nika.service.kathara import KatharaFRRAPI
from nika.service.kathara.base_api import KatharaBaseAPI
from nika.utils.session_store import SessionStore
from nika.workflows.eval.session import run_eval_metrics
from nika.workflows.session.close import close_session
from tests.agent._assertions import (
    _extract_tool_names,
    assert_phase_messages,
    assert_submission_fields,
)
from tests.support.integration_base import IntegrationTestCase, OrderedPipelineTestCase
from tests.support.integration_pipeline import (
    deepseek_api_key_available,
    load_test_env,
)
from tests.support.prerequisites import docker_available

load_test_env()

PROBLEM = "bgp_rpki_invalid_route_leak"
ENV_ARGS: list[str] = []
AGENT_MAX_STEPS = 40
_BGP_RPKI_TOOLS = (
    "frr_get_rpki_status",
    "frr_exec",
    "frr_get_bgp_conf",
    "frr_show_ip_route",
    "traceroute",
    "ping_pair",
)


def _rpki_roles() -> dict[str, str]:
    isp_plan = compile_isp_plan(IspConfig(topology="abilene", igp="ospf"))
    bgp = compile_bgp_plan(isp_plan, "ebgp", rpki=True)
    assert bgp is not None and bgp.inventory.get("rpki")
    return {
        "leaker": str(bgp.inventory["leaker_device"]),
        "rov": str(bgp.inventory["rov_observer"]),
        "non_rov": str(bgp.inventory["non_rov_observer"]),
        "leaker_asn": str(bgp.inventory["leaker_asn"]),
    }


def _inject_params() -> dict[str, str]:
    roles = _rpki_roles()
    return {"host_name": roles["leaker"]}


def _tool_error_events(messages: list[dict]) -> list[dict]:
    return [e for e in messages if e.get("event") == "tool_error"]


def _diagnosis_tool_names(messages: list[dict]) -> list[str]:
    from agent.protocols import DIAGNOSIS

    names: list[str] = []
    for entry in messages:
        if entry["phase"] != DIAGNOSIS:
            continue
        names.extend(n for n in _extract_tool_names(entry) if n)
    return names


def _assert_bgp_rpki_tool_use(messages: list[dict]) -> None:
    diag_tools = _diagnosis_tool_names(messages)
    assert diag_tools, "diagnosis phase must call MCP tools"
    hits = [n for n in diag_tools if any(t in n for t in _BGP_RPKI_TOOLS)]
    assert hits, (
        f"diagnosis must call BGP/RPKI telemetry tools; saw {sorted(set(diag_tools))}"
    )


def _assert_no_tool_errors(messages: list[dict]) -> None:
    errors = _tool_error_events(messages)
    assert not errors, f"MCP tool errors during diagnosis/submission: {errors[:5]}"


def _assert_rca_correct(session_dir: Path, leaker: str) -> None:
    assert_submission_fields(session_dir)
    submission = json.loads((session_dir / "submission.json").read_text())
    causes = submission.get("root_causes") or []
    assert causes, "submission root_causes empty"
    expected = f"node/{leaker}"
    matched = False
    for item in causes:
        resource_id = item.get("resource_id") or (item.get("resource") or {}).get(
            "id", ""
        )
        fault_type = item.get("fault_type") or ""
        if resource_id == expected and fault_type == PROBLEM:
            matched = True
            break
    assert matched, (
        f"expected RCA {expected} + {PROBLEM}; got {json.dumps(causes, ensure_ascii=False)}"
    )


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class TestBGPRPKIInvalidRouteLeakE2E(IntegrationTestCase):
    """Deploy Abilene eBGP, inject leak, verify observer divergence (≥3 runs)."""

    @pytest.mark.parametrize("_run_idx", range(3))
    def test_rpki_invalid_route_leak_cycle(self, _run_idx: int) -> None:
        roles = _rpki_roles()
        params = {"host_name": roles["leaker"]}

        session_id = self._start_env("isp_abilene_ebgp_rpki", ENV_ARGS)
        try:
            self._assert_session_ready(session_id, "isp_abilene_ebgp_rpki")
            row = self._session_row(session_id)
            lab_name = row["lab_name"]

            time.sleep(30)
            self._inject_failure(PROBLEM, params, session_id=session_id)
            self._assert_failure_injected(PROBLEM, session_id=session_id)

            time.sleep(15)
            frr = KatharaFRRAPI(lab_name=lab_name)
            base = KatharaBaseAPI(lab_name=lab_name)

            summary = frr.frr_get_routing_state(roles["leaker"])
            assert summary.strip()

            routes_non_rov = frr.frr_get_routing_state(
                roles["non_rov"], prefix="203.0.113.0/24"
            )
            assert "203.0.113" in routes_non_rov
            assert roles["leaker_asn"] in routes_non_rov

            routes_rov = frr.frr_get_routing_state(
                roles["rov"], prefix="203.0.113.0/24"
            )
            absent = (
                "Network not in table" in routes_rov or "203.0.113" not in routes_rov
            )
            invalid_not_best = "Invalid" in routes_rov and "*" not in routes_rov
            assert absent or invalid_not_best, routes_rov

            rpki = frr.frr_get_rpki_status(roles["rov"], prefix="203.0.113.0/24")
            assert rpki.strip()
            neighbors = frr.frr_get_routing_state(roles["leaker"])
            assert neighbors.strip()
            tr = base.traceroute(roles["non_rov"], "203.0.113.1")
            assert tr is not None
        finally:
            self._close_session(session_id)


class _BGPRPKIAgentPipelineBase(OrderedPipelineTestCase):
    """Real byo.mcp_agent on injected Abilene RPKI lab; check tools + RCA."""

    llm_provider: str = ""
    model: str = ""
    session_id: str | None = None
    session_dir: Path | None = None
    env_destroyed: bool = False
    leaker: str = ""

    def test_step_01_start_env(self) -> None:
        type(self).session_id = self._start_env("isp_abilene_ebgp_rpki", ENV_ARGS)
        self._assert_session_ready(self.session_id, "isp_abilene_ebgp_rpki")
        time.sleep(30)

    def test_step_02_inject_failure(self) -> None:
        assert self.session_id is not None
        params = _inject_params()
        type(self).leaker = params["host_name"]
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
        _assert_bgp_rpki_tool_use(messages)
        _assert_no_tool_errors(messages)

    def test_step_05_check_rca_and_eval(self) -> None:
        assert self.session_id is not None
        assert self.session_dir is not None
        _assert_rca_correct(self.session_dir, self.leaker)
        close_session(session_id=self.session_id)
        type(self).env_destroyed = True
        run_eval_metrics(session_id=self.session_id)
        metrics = self._load_json("eval_metrics.json")
        assert metrics.get("detection_score", 0) >= 1.0
        assert metrics.get("localization_accuracy", 0) >= 1.0
        assert metrics.get("rca_accuracy", 0) >= 1.0
        assert metrics.get("tool_calls", 0) >= 1


@pytest.mark.skipif(
    not (docker_available() and deepseek_api_key_available()),
    reason="Docker and DEEPSEEK_API_KEY required for RPKI agent e2e",
)
class TestBGPRPKIInvalidRouteLeakAgentDeepseek(_BGPRPKIAgentPipelineBase):
    llm_provider = "deepseek"
    model = "deepseek-chat"
