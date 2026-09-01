"""Parametrized host-side agent pipeline contract (byo.mcp_agent).

Includes background traffic before the live agent so diagnosis runs under load.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from nika.cli.main import app
from nika.utils.session_store import SessionStore
from tests.agent._assertions import assert_phase_messages
from tests.support.integration_base import OrderedPipelineTestCase
from tests.support.integration_pipeline import (
    ClabCommonPipelineSteps,
    CommonPipelineSteps,
    _min3clos_prerequisites,
    deepseek_api_key_available,
    load_test_env,
)

load_test_env()

MCP_AGENT_MAX_STEPS = 20
_TRAFFIC_ARGS = [
    "traffic",
    "run",
    "od",
    "--mesh-mbps",
    "1",
    "--interval",
    "3",
    "--background",
]


def _run_background_od_traffic(session_id: str) -> None:
    import os

    prev = os.environ.get("NIKA_SESSION_ID")
    os.environ["NIKA_SESSION_ID"] = session_id
    try:
        result = CliRunner().invoke(app, _TRAFFIC_ARGS)
        assert result.exit_code == 0, result.output
    finally:
        if prev is None:
            os.environ.pop("NIKA_SESSION_ID", None)
        else:
            os.environ["NIKA_SESSION_ID"] = prev


class _McpAgentPipelineBase(CommonPipelineSteps, OrderedPipelineTestCase):
    llm_provider: str = ""
    model: str = ""

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_02b_traffic_run(self) -> None:
        assert self.session_id is not None
        _run_background_od_traffic(self.session_id)

    def test_step_03_run_mcp_agent(self) -> None:
        assert self.session_id is not None
        self._run_agent(
            agent_type="byo.mcp_agent",
            llm_provider=self.llm_provider,
            model=self.model,
            max_steps=MCP_AGENT_MAX_STEPS,
        )
        row = SessionStore().get_session(self.session_id)
        assert row.get("agent_type") == "byo.mcp_agent"

    def test_step_04_check_messages(self) -> None:
        assert self.session_dir is not None
        assert_phase_messages(
            self._load_jsonl("messages.jsonl"),
            require_submission_tools=False,
        )

    def test_step_05_session_close(self) -> None:
        self._step_close_and_verify("byo.mcp_agent")


class _McpAgentClabPipelineBase(ClabCommonPipelineSteps, OrderedPipelineTestCase):
    llm_provider: str = ""
    model: str = ""

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_02b_traffic_run(self) -> None:
        assert self.session_id is not None
        _run_background_od_traffic(self.session_id)

    def test_step_03_run_mcp_agent(self) -> None:
        assert self.session_id is not None
        self._run_agent(
            agent_type="byo.mcp_agent",
            llm_provider=self.llm_provider,
            model=self.model,
            max_steps=MCP_AGENT_MAX_STEPS,
        )
        row = SessionStore().get_session(self.session_id)
        assert row.get("agent_type") == "byo.mcp_agent"

    def test_step_04_check_messages(self) -> None:
        assert self.session_dir is not None
        assert_phase_messages(
            self._load_jsonl("messages.jsonl"),
            require_submission_tools=False,
        )

    def test_step_05_session_close(self) -> None:
        self._step_close_and_verify("byo.mcp_agent")


@pytest.mark.live
@pytest.mark.e2e
@pytest.mark.skipif(
    not deepseek_api_key_available(),
    reason="DEEPSEEK_API_KEY required for byo.mcp_agent live e2e",
)
class McpAgentKatharaPipelineTest(_McpAgentPipelineBase):
    llm_provider = "deepseek"
    model = "deepseek-chat"


@pytest.mark.live
@pytest.mark.e2e
@pytest.mark.skipif(
    not (_min3clos_prerequisites() and deepseek_api_key_available()),
    reason="containerlab/gnmic/Docker or DEEPSEEK_API_KEY not available",
)
class McpAgentClabPipelineTest(_McpAgentClabPipelineBase):
    llm_provider = "deepseek"
    model = "deepseek-chat"
