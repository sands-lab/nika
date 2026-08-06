from __future__ import annotations
import pytest
from nika.utils.session_store import SessionStore
from tests.agent._assertions import assert_phase_messages, assert_submission_fields
from tests.support.integration_base import OrderedPipelineTestCase
from tests.support.integration_pipeline import (
    ClabCommonPipelineSteps,
    CommonPipelineSteps,
    _min3clos_prerequisites,
    deepseek_api_key_available,
    load_test_env,
)

load_test_env()
MCP_AGENT_PROVIDER = "deepseek"
MCP_AGENT_MODEL = "deepseek-chat"
MCP_AGENT_MAX_STEPS = 20


@pytest.mark.skipif(
    not deepseek_api_key_available(),
    reason="DEEPSEEK_API_KEY required for byo.mcp_agent",
)
class McpAgentPipelineTest(CommonPipelineSteps, OrderedPipelineTestCase):
    """Full pipeline with the mcp-agent SDK agent using DeepSeek."""

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_03_run_mcp_agent(self) -> None:
        assert self.session_id is not None
        self._run_agent(
            agent_type="byo.mcp_agent",
            llm_provider=MCP_AGENT_PROVIDER,
            model=MCP_AGENT_MODEL,
            max_steps=MCP_AGENT_MAX_STEPS,
        )
        row = SessionStore().get_session(self.session_id)
        assert row.get("agent_type") == "byo.mcp_agent"

    def test_step_04_check_messages(self) -> None:
        assert self.session_dir is not None
        assert_phase_messages(self._load_jsonl("messages.jsonl"))

    def test_step_05_check_submission(self) -> None:
        assert self.session_dir is not None
        assert_submission_fields(self.session_dir)

    def test_step_06_session_close(self) -> None:
        self._step_close_and_verify("byo.mcp_agent")

    def test_step_07_eval_metrics(self) -> None:
        self._step_eval_metrics()


@pytest.mark.skipif(
    not (_min3clos_prerequisites() and deepseek_api_key_available()),
    reason="containerlab/gnmic/Docker or DEEPSEEK_API_KEY not available",
)
class McpAgentClabPipelineTest(ClabCommonPipelineSteps, OrderedPipelineTestCase):
    """Full containerlab pipeline with the mcp-agent SDK agent."""

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_03_run_mcp_agent(self) -> None:
        assert self.session_id is not None
        self._run_agent(
            agent_type="byo.mcp_agent",
            llm_provider=MCP_AGENT_PROVIDER,
            model=MCP_AGENT_MODEL,
            max_steps=MCP_AGENT_MAX_STEPS,
        )
        row = SessionStore().get_session(self.session_id)
        assert row.get("agent_type") == "byo.mcp_agent"

    def test_step_04_check_messages(self) -> None:
        assert self.session_dir is not None
        assert_phase_messages(self._load_jsonl("messages.jsonl"))

    def test_step_05_check_submission(self) -> None:
        assert self.session_dir is not None
        assert_submission_fields(self.session_dir)

    def test_step_06_session_close(self) -> None:
        self._step_close_and_verify("byo.mcp_agent")

    def test_step_07_eval_metrics(self) -> None:
        self._step_eval_metrics()
