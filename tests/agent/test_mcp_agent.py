from __future__ import annotations
import pytest
from nika.utils.session_store import SessionStore
from tests.agent._assertions import assert_phase_messages
from tests.support.integration_base import OrderedPipelineTestCase
from tests.support.integration_pipeline import (
    ClabCommonPipelineSteps,
    CommonPipelineSteps,
    _min3clos_prerequisites,
    anthropic_api_key_available,
    deepseek_api_key_available,
    load_test_env,
)

load_test_env()
MCP_AGENT_MAX_STEPS = 20


class _McpAgentPipelineBase(CommonPipelineSteps, OrderedPipelineTestCase):
    """Shared Kathara pipeline for byo.mcp_agent."""

    llm_provider: str = ""
    model: str = ""

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

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
    """Shared Containerlab pipeline for byo.mcp_agent."""

    llm_provider: str = ""
    model: str = ""

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

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


@pytest.mark.skipif(
    not deepseek_api_key_available(),
    reason="DEEPSEEK_API_KEY required for byo.mcp_agent deepseek e2e",
)
class McpAgentDeepseekPipelineTest(_McpAgentPipelineBase):
    """mcp-agent via DeepSeek OpenAI-compatible API."""

    llm_provider = "deepseek"
    model = "deepseek-chat"


@pytest.mark.skipif(
    not anthropic_api_key_available(),
    reason="ANTHROPIC_API_KEY required for byo.mcp_agent anthropic e2e",
)
class McpAgentAnthropicPipelineTest(_McpAgentPipelineBase):
    """mcp-agent via official Anthropic API."""

    llm_provider = "anthropic"
    model = "claude-haiku-4-5"


@pytest.mark.skipif(
    not (_min3clos_prerequisites() and deepseek_api_key_available()),
    reason="containerlab/gnmic/Docker or DEEPSEEK_API_KEY not available",
)
class McpAgentDeepseekClabPipelineTest(_McpAgentClabPipelineBase):
    llm_provider = "deepseek"
    model = "deepseek-chat"


@pytest.mark.skipif(
    not (_min3clos_prerequisites() and anthropic_api_key_available()),
    reason="containerlab/gnmic/Docker or ANTHROPIC_API_KEY not available",
)
class McpAgentAnthropicClabPipelineTest(_McpAgentClabPipelineBase):
    llm_provider = "anthropic"
    model = "claude-haiku-4-5"
