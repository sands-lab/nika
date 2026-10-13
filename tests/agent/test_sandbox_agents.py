from __future__ import annotations

import json

import pytest
from agent.protocols import DIAGNOSIS
from nika.run_config.loader import reset_run_config, set_run_config
from nika.run_config.schema import RunConfig
from nika.utils.session_store import SessionStore
from tests.agent._assertions import (
    assert_phase_messages,
    assert_no_codex_tool_item_mirrors,
)
from tests.agent.sandbox_support import (
    sandbox_provider_credential_available,
    sandbox_runtime_available,
)
from tests.support.integration_base import OrderedPipelineTestCase
from tests.support.integration_pipeline import (
    CommonPipelineSteps,
    claude_cli_available,
    claude_sdk_available,
    codex_cli_available,
    codex_sdk_available,
    live_anthropic_test_config,
    live_codex_test_model,
    load_test_env,
    sade_available,
)

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox, pytest.mark.live]

load_test_env()
CLAUDE_PROVIDER, CLAUDE_MODEL = live_anthropic_test_config()
# Codex CLI/SDK use OpenAI API keys or a ChatGPT login stored in sbx.
CODEX_MODEL = live_codex_test_model()
MAX_STEPS = 20
_SANDBOX_SKIP = not sandbox_runtime_available()
_SENSITIVE_NAMES = ("auth.json", ".credentials.json", ".host_auth")


def _isolate_agent_run_config(*, agent_type: str, provider: str, model: str) -> None:
    """Avoid host config/nika.yaml base_url leaking into provider env."""
    reset_run_config()
    set_run_config(
        RunConfig.model_validate(
            {
                "nika": {
                    "result_dir": "results/test",
                    "sandbox": {
                        "offline_sdk_wheels": True,
                    },
                },
                "agent": {
                    "type": agent_type,
                    "provider": provider,
                    "model": model,
                    "max_steps": MAX_STEPS,
                    "base_url": None,
                },
            }
        )
    )


class SandboxAgentPipelineBase(CommonPipelineSteps, OrderedPipelineTestCase):
    agent_type: str = ""
    llm_provider: str = CLAUDE_PROVIDER
    model: str = CLAUDE_MODEL
    _agent_run_failed: bool = False

    def setup_method(self) -> None:
        _isolate_agent_run_config(
            agent_type=self.agent_type,
            provider=self.llm_provider,
            model=self.model,
        )

    def teardown_method(self) -> None:
        reset_run_config()

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
                llm_provider=self.llm_provider,
                model=self.model,
                max_steps=MAX_STEPS,
                reasoning_effort="low" if "codex" in self.agent_type else None,
            )
            row = SessionStore().get_session(self.session_id)
            assert row.get("agent_type") == self.agent_type
        except FileNotFoundError as exc:
            type(self)._agent_run_failed = True
            pytest.skip(f"Session store missing during agent run: {exc}")

    def _assert_sandbox_artifacts(self) -> None:
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

    def test_step_04_check_sandbox_artifacts(self) -> None:
        if type(self)._agent_run_failed:
            pytest.skip("Skipping sandbox artifact checks due to agent-run failure")
        self._assert_sandbox_artifacts()
        messages = self._load_jsonl("messages.jsonl")
        assert_phase_messages(
            messages,
            require_diagnosis_tools=True,
            require_submission_tools=False,
        )
        assert_no_codex_tool_item_mirrors(messages)
        phases = {e["phase"] for e in messages}
        assert DIAGNOSIS in phases

    def test_step_05_session_close(self) -> None:
        if type(self)._agent_run_failed:
            pytest.skip("Skipping session close verification due to agent-run failure")
        self._step_close_and_verify(self.agent_type)


@pytest.mark.skipif(_SANDBOX_SKIP, reason="Docker Sandboxes runtime not available")
@pytest.mark.skipif(
    not (codex_cli_available() and sandbox_provider_credential_available("openai")),
    reason="Codex CLI + OPENAI_API_KEY or sbx openai secret required",
)
class SandboxCodexCliPipelineTest(SandboxAgentPipelineBase):
    agent_type = "cli.codex"
    llm_provider = "openai"
    model = CODEX_MODEL


@pytest.mark.skipif(_SANDBOX_SKIP, reason="Docker Sandboxes runtime not available")
@pytest.mark.skipif(
    not (
        claude_cli_available()
        and sandbox_provider_credential_available(CLAUDE_PROVIDER)
    ),
    reason=f"Claude CLI + {CLAUDE_PROVIDER} credentials required (DeepSeek: DEEPSEEK_API_KEY; Anthropic: API key or sbx anthropic secret)",
)
class SandboxClaudeCliPipelineTest(SandboxAgentPipelineBase):
    agent_type = "cli.claude"
    model = CLAUDE_MODEL


@pytest.mark.skipif(_SANDBOX_SKIP, reason="Docker Sandboxes runtime not available")
@pytest.mark.skipif(
    not (codex_sdk_available() and sandbox_provider_credential_available("openai")),
    reason="Codex SDK + OPENAI_API_KEY or sbx openai secret required",
)
class SandboxCodexSdkPipelineTest(SandboxAgentPipelineBase):
    agent_type = "sdk.codex_sdk"
    llm_provider = "openai"
    model = CODEX_MODEL


@pytest.mark.skipif(_SANDBOX_SKIP, reason="Docker Sandboxes runtime not available")
@pytest.mark.skipif(
    not (
        claude_sdk_available()
        and sandbox_provider_credential_available(CLAUDE_PROVIDER)
    ),
    reason=f"Claude SDK + {CLAUDE_PROVIDER} credentials required (DeepSeek: DEEPSEEK_API_KEY; Anthropic: API key or sbx anthropic secret)",
)
class SandboxClaudeSdkPipelineTest(SandboxAgentPipelineBase):
    agent_type = "sdk.claude_sdk"
    model = CLAUDE_MODEL


@pytest.mark.skipif(_SANDBOX_SKIP, reason="Docker Sandboxes runtime not available")
@pytest.mark.skipif(
    not (sade_available() and sandbox_provider_credential_available(CLAUDE_PROVIDER)),
    reason=f"SADE + {CLAUDE_PROVIDER} credentials required (DeepSeek: DEEPSEEK_API_KEY; Anthropic: API key or sbx anthropic secret)",
)
class SandboxSadePipelineTest(SandboxAgentPipelineBase):
    agent_type = "community.sade"
    model = CLAUDE_MODEL

    def test_step_04_check_sandbox_artifacts(self) -> None:
        if type(self)._agent_run_failed:
            pytest.skip("Skipping sandbox artifact checks due to agent-run failure")
        self._assert_sandbox_artifacts()
        messages = self._load_jsonl("messages.jsonl")
        assert messages, "SADE must emit messages.jsonl"
        tool_starts = [e for e in messages if e.get("event") == "tool_start"]
        assert tool_starts, "SADE must emit tool_start events"
