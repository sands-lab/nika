from __future__ import annotations
import pytest
import unittest.mock
from agent.cli.codex.codex_worker import _build_mcp_toml
from agent.sdk.codex_sdk.config import (
    codex_sdk_local_auth_available,
    validate_reasoning_effort,
)
from nika.utils.session_store import SessionStore
from tests.agent._assertions import assert_phase_messages, assert_submission_fields
from tests.agent.sandbox_support import SANDBOX_E2E_SUPERSEDED
from tests.support.integration_base import OrderedPipelineTestCase
from tests.support.integration_pipeline import (
    ClabCommonPipelineSteps,
    CommonPipelineSteps,
    _min3clos_prerequisites,
    codex_sdk_available,
    load_test_env,
)

load_test_env()
CODEX_MODEL = "gpt-5.4-mini"


class CodexSdkConfigTest:
    """Local auth and reasoning-effort validation for sdk.codex_sdk."""

    def test_validate_reasoning_effort_accepts_valid(self) -> None:
        assert validate_reasoning_effort("medium") == "medium"

    def test_validate_reasoning_effort_rejects_invalid(self) -> None:
        with pytest.raises(ValueError):
            validate_reasoning_effort("invalid")

    def test_local_auth_detection(self) -> None:
        with unittest.mock.patch("agent.sdk.codex_sdk.config.Path") as mock_path:
            mock_home = mock_path.home.return_value
            mock_home.__truediv__.return_value.is_file.return_value = True
            assert codex_sdk_local_auth_available()


class CodexSdkMcpTest:
    """MCP config TOML generation reused from cli.codex."""

    def test_includes_mcp_server_section(self) -> None:
        toml = _build_mcp_toml(
            {
                "kathara_base_mcp_server": {
                    "command": "python3",
                    "args": ["/path/base.py"],
                    "env": {"NIKA_SESSION_ID": "sess-abc"},
                }
            }
        )
        assert "[mcp_servers.kathara_base_mcp_server]" in toml
        assert 'NIKA_SESSION_ID = "sess-abc"' in toml


@SANDBOX_E2E_SUPERSEDED
@pytest.mark.skipif(
    not codex_sdk_available(), reason="openai-codex + ~/.codex/auth.json required"
)
class CodexSdkAgentPipelineTest(CommonPipelineSteps, OrderedPipelineTestCase):
    """Full pipeline with the sdk.codex_sdk agent."""

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_03_run_codex_sdk_agent(self) -> None:
        assert self.session_id is not None
        self._run_agent(agent_type="sdk.codex_sdk", model=CODEX_MODEL, max_steps=20)
        row = SessionStore().get_session(self.session_id)
        assert row.get("agent_type") == "sdk.codex_sdk"

    def test_step_04_check_messages(self) -> None:
        assert self.session_dir is not None
        assert_phase_messages(self._load_jsonl("messages.jsonl"))

    def test_step_05_check_submission(self) -> None:
        assert self.session_dir is not None
        assert (self.session_dir / "submission.json").exists()
        assert_submission_fields(self.session_dir)

    def test_step_06_session_close(self) -> None:
        self._step_close_and_verify("sdk.codex_sdk")

    def test_step_07_eval_metrics(self) -> None:
        self._step_eval_metrics()


@SANDBOX_E2E_SUPERSEDED
@pytest.mark.skipif(
    not (_min3clos_prerequisites() and codex_sdk_available()),
    reason="containerlab/gnmic/Docker or openai-codex credentials not available",
)
class CodexSdkClabPipelineTest(ClabCommonPipelineSteps, OrderedPipelineTestCase):
    """Full containerlab pipeline with the sdk.codex_sdk agent."""

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_03_run_codex_sdk_agent(self) -> None:
        assert self.session_id is not None
        self._run_agent(agent_type="sdk.codex_sdk", model=CODEX_MODEL, max_steps=20)
        row = SessionStore().get_session(self.session_id)
        assert row.get("agent_type") == "sdk.codex_sdk"

    def test_step_04_check_messages(self) -> None:
        assert self.session_dir is not None
        assert_phase_messages(self._load_jsonl("messages.jsonl"))

    def test_step_05_check_submission(self) -> None:
        assert self.session_dir is not None
        assert (self.session_dir / "submission.json").exists()
        assert_submission_fields(self.session_dir)

    def test_step_06_session_close(self) -> None:
        self._step_close_and_verify("sdk.codex_sdk")

    def test_step_07_eval_metrics(self) -> None:
        self._step_eval_metrics()
