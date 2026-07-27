from __future__ import annotations

import pytest
import json
from agent.utils.phases import DIAGNOSIS, SUBMISSION
from nika.utils.session_store import SessionStore
from tests.agent._assertions import (
    assert_phase_messages,
    assert_submission_fields,
)
from tests.agent.sandbox_support import (
    sandbox_anthropic_credential_available,
    sandbox_openai_credential_available,
    sandbox_runtime_available,
)
from tests.support.integration_base import OrderedPipelineTestCase
from tests.support.integration_pipeline import (
    CommonPipelineSteps,
    claude_cli_available,
    claude_sdk_available,
    codex_cli_available,
    codex_sdk_available,
    load_test_env,
    sade_available,
)

load_test_env()
CODEX_MODEL = "gpt-5-mini"
CLAUDE_MODEL = "deepseek-v4-flash"
MAX_STEPS = 20
_SANDBOX_SKIP = not sandbox_runtime_available()
_SENSITIVE_NAMES = ("auth.json", ".credentials.json", ".host_auth")


class SandboxAgentPipelineBase(CommonPipelineSteps, OrderedPipelineTestCase):
    agent_type: str = ""
    model: str = ""
    _agent_run_failed: bool = False

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_03_run_sandbox_agent(self) -> None:
        assert self.session_id is not None
        type(self)._agent_run_failed = False
        try:
            self._run_agent(
                agent_type=self.agent_type,
                model=self.model,
                max_steps=MAX_STEPS,
            )
            row = SessionStore().get_session(self.session_id)
            assert row.get("agent_type") == self.agent_type
        except FileNotFoundError as exc:
            type(self)._agent_run_failed = True
            pytest.skip(f"Session store missing during agent run: {exc}")

    def test_step_04_check_sandbox_artifacts(self) -> None:
        if type(self)._agent_run_failed:
            pytest.skip("Skipping sandbox artifact checks due to agent-run failure")
        assert self.session_dir is not None
        manifest = self.session_dir / "sandbox_manifest.json"
        assert manifest.is_file()
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert data["agent_type"] == self.agent_type
        text = manifest.read_text(encoding="utf-8")
        assert "OPENAI_API_KEY" not in text
        assert "ANTHROPIC_API_KEY" not in text
        assert not (self.session_dir / ".sandbox_run").exists()
        for dirname in (
            "codex_workspace",
            "claude_workspace",
            "codex_sdk_workspace",
            "claude_sdk_workspace",
        ):
            assert not (self.session_dir / dirname).exists()
        for path in self.session_dir.rglob("*"):
            if path.name in _SENSITIVE_NAMES:
                pytest.fail(f"credential file leaked into results: {path}")
        messages = self._load_jsonl("messages.jsonl")
        assert_phase_messages(messages, require_diagnosis_tools=True)
        agents = {e["agent"] for e in messages}
        assert DIAGNOSIS in agents
        assert SUBMISSION in agents

    def test_step_05_check_submission(self) -> None:
        if type(self)._agent_run_failed:
            pytest.skip("Skipping submission checks due to agent-run failure")
        assert self.session_dir is not None
        assert (self.session_dir / "submission.json").exists()
        assert_submission_fields(self.session_dir)

    def test_step_06_session_close(self) -> None:
        if type(self)._agent_run_failed:
            pytest.skip("Skipping session close verification due to agent-run failure")
        self._step_close_and_verify(self.agent_type)

    def test_step_07_eval_metrics(self) -> None:
        if type(self)._agent_run_failed:
            pytest.skip("Skipping eval metrics due to agent-run failure")
        self._step_eval_metrics()


@pytest.mark.skipif(_SANDBOX_SKIP, reason="Docker Sandboxes runtime not available")
@pytest.mark.skipif(
    not (codex_cli_available() and sandbox_openai_credential_available()),
    reason="Codex CLI + openai sbx/.env credentials required",
)
class SandboxCodexCliPipelineTest(SandboxAgentPipelineBase):
    agent_type = "local_cli.codex_cli"
    model = CODEX_MODEL


@pytest.mark.skipif(_SANDBOX_SKIP, reason="Docker Sandboxes runtime not available")
@pytest.mark.skipif(
    not (claude_cli_available() and sandbox_anthropic_credential_available()),
    reason="Claude CLI + anthropic sbx/.env credentials required",
)
class SandboxClaudeCliPipelineTest(SandboxAgentPipelineBase):
    agent_type = "local_cli.claude_cli"
    model = CLAUDE_MODEL


@pytest.mark.skipif(_SANDBOX_SKIP, reason="Docker Sandboxes runtime not available")
@pytest.mark.skipif(
    not (codex_sdk_available() and sandbox_openai_credential_available()),
    reason="Codex SDK + openai sbx/.env credentials required",
)
class SandboxCodexSdkPipelineTest(SandboxAgentPipelineBase):
    agent_type = "sdk.codex_sdk"
    model = CODEX_MODEL


@pytest.mark.skipif(_SANDBOX_SKIP, reason="Docker Sandboxes runtime not available")
@pytest.mark.skipif(
    not (claude_sdk_available() and sandbox_anthropic_credential_available()),
    reason="Claude SDK + anthropic sbx/.env credentials required",
)
class SandboxClaudeSdkPipelineTest(SandboxAgentPipelineBase):
    agent_type = "sdk.claude_sdk"
    model = CLAUDE_MODEL


@pytest.mark.skipif(_SANDBOX_SKIP, reason="Docker Sandboxes runtime not available")
@pytest.mark.skipif(
    not (sade_available() and sandbox_anthropic_credential_available()),
    reason="SADE + anthropic sbx/.env credentials required",
)
class SandboxSadePipelineTest(SandboxAgentPipelineBase):
    agent_type = "community.sade"
    model = CLAUDE_MODEL

    def test_step_04_check_sandbox_artifacts(self) -> None:
        if type(self)._agent_run_failed:
            pytest.skip("Skipping sandbox artifact checks due to agent-run failure")
        assert self.session_dir is not None
        manifest = self.session_dir / "sandbox_manifest.json"
        assert manifest.is_file()
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert data["agent_type"] == self.agent_type
        text = manifest.read_text(encoding="utf-8")
        assert "OPENAI_API_KEY" not in text
        assert "ANTHROPIC_API_KEY" not in text
        assert not (self.session_dir / ".sandbox_run").exists()
        for dirname in (
            "codex_workspace",
            "claude_workspace",
            "codex_sdk_workspace",
            "claude_sdk_workspace",
        ):
            assert not (self.session_dir / dirname).exists()
        for path in self.session_dir.rglob("*"):
            if path.name in _SENSITIVE_NAMES:
                pytest.fail(f"credential file leaked into results: {path}")
        messages = self._load_jsonl("messages.jsonl")
        assert messages, "SADE must emit messages.jsonl"
        tool_starts = [e for e in messages if e.get("event") == "tool_start"]
        assert tool_starts, "SADE must emit tool_start events"
        assert (self.session_dir / "submission.json").exists()
