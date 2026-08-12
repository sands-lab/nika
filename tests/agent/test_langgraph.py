from __future__ import annotations
import pytest
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
LANGGRAPH_PROVIDER = "deepseek"
LANGGRAPH_MODEL = "deepseek-chat"
LANGGRAPH_MAX_STEPS = 40
LANGGRAPH_CLAB_MAX_STEPS = 60


@pytest.mark.skipif(
    not deepseek_api_key_available(),
    reason="DEEPSEEK_API_KEY required for byo.langgraph agent",
)
class LangGraphAgentPipelineTest(CommonPipelineSteps, OrderedPipelineTestCase):
    """Full pipeline with the LangGraph agent."""

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_03_run_langgraph_agent(self) -> None:
        assert self.session_id is not None
        self._run_agent(
            agent_type="byo.langgraph",
            llm_provider=LANGGRAPH_PROVIDER,
            model=LANGGRAPH_MODEL,
            max_steps=LANGGRAPH_MAX_STEPS,
        )
        row = SessionStore().get_session(self.session_id)
        assert row.get("agent_type") == "byo.langgraph"

    def test_step_04_check_messages(self) -> None:
        assert self.session_dir is not None
        assert_phase_messages(
            self._load_jsonl("messages.jsonl"),
            require_submission_tools=False,
        )

    def test_step_05_session_close(self) -> None:
        self._step_close_and_verify("byo.langgraph")


@pytest.mark.skipif(
    not (_min3clos_prerequisites() and deepseek_api_key_available()),
    reason="containerlab/gnmic/Docker or DEEPSEEK_API_KEY not available",
)
class LangGraphClabPipelineTest(ClabCommonPipelineSteps, OrderedPipelineTestCase):
    """Full containerlab pipeline with the LangGraph agent."""

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_03_run_langgraph_agent(self) -> None:
        assert self.session_id is not None
        self._run_agent(
            agent_type="byo.langgraph",
            llm_provider=LANGGRAPH_PROVIDER,
            model=LANGGRAPH_MODEL,
            max_steps=LANGGRAPH_CLAB_MAX_STEPS,
        )
        row = SessionStore().get_session(self.session_id)
        assert row.get("agent_type") == "byo.langgraph"

    def test_step_04_check_messages(self) -> None:
        assert self.session_dir is not None
        assert_phase_messages(
            self._load_jsonl("messages.jsonl"),
            require_submission_tools=False,
        )

    def test_step_05_session_close(self) -> None:
        self._step_close_and_verify("byo.langgraph")
