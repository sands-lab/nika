from __future__ import annotations

import functools
import json

import pytest
from agent.protocols import DIAGNOSIS
from nika.run_config.loader import (
    load_run_config,
    reset_run_config,
    set_run_config,
)
from nika.run_config.schema import RunConfig
from nika.utils.session_store import SessionStore
from tests.agent._assertions import (
    assert_phase_messages,
    assert_no_codex_tool_item_mirrors,
)
from tests.agent.sandbox_support import (
    sandbox_backend_available,
    sandbox_provider_credential_available,
)
from tests.support.integration_base import OrderedPipelineTestCase
from tests.support.integration_pipeline import (
    CommonPipelineSteps,
    claude_cli_available,
    codex_cli_available,
    live_anthropic_test_config,
    live_codex_test_model,
    live_custom_base_url,
    load_test_env,
    sade_available,
)

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox, pytest.mark.live]

load_test_env()
CLAUDE_PROVIDER, CLAUDE_MODEL = live_anthropic_test_config()
# Codex CLI/SDK use OpenAI API keys or a ChatGPT login stored in sbx.
CODEX_MODEL = live_codex_test_model()
MAX_STEPS = 20
_backend_available = functools.cache(sandbox_backend_available)
_SENSITIVE_NAMES = ("auth.json", ".credentials.json", ".host_auth")


def _isolate_agent_run_config(
    *, agent_type: str, provider: str, model: str, backend: str
) -> None:
    """Avoid host config/nika.yaml base_url leaking into provider env.

    Keeps the host's E2B service address (``nika.sandbox.url``), so the E2B
    variants run against the configured service, such as AgentENV.
    """
    reset_run_config()
    sandbox: dict[str, object] = {"backend": backend, "offline_sdk_wheels": True}
    if backend == "e2b":
        sandbox["url"] = load_run_config().nika.sandbox.url
    set_run_config(
        RunConfig.model_validate(
            {
                "nika": {
                    "result_dir": "results/test",
                    "sandbox": sandbox,
                },
                "agent": {
                    "type": agent_type,
                    "provider": provider,
                    "model": model,
                    "max_steps": MAX_STEPS,
                    "base_url": live_custom_base_url()
                    if provider == "custom"
                    else None,
                },
            }
        )
    )


class SandboxAgentPipelineBase(CommonPipelineSteps, OrderedPipelineTestCase):
    agent_type: str = ""
    sandbox_backend: str = "sbx"
    llm_provider: str = CLAUDE_PROVIDER
    model: str = CLAUDE_MODEL
    _agent_run_failed: bool = False

    def setup_method(self) -> None:
        backend = self.sandbox_backend
        if not _backend_available(backend):
            pytest.skip(f"{backend} sandbox runtime not available")
        if not sandbox_provider_credential_available(
            self.llm_provider, backend=backend
        ):
            pytest.skip(f"{self.llm_provider} credentials for {backend} not available")
        _isolate_agent_run_config(
            agent_type=self.agent_type,
            provider=self.llm_provider,
            model=self.model,
            backend=backend,
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


@pytest.mark.skipif(not codex_cli_available(), reason="Codex CLI not available")
class SandboxCodexCliPipelineTest(SandboxAgentPipelineBase):
    agent_type = "cli.codex"
    llm_provider = "openai"
    model = CODEX_MODEL


@pytest.mark.skipif(not claude_cli_available(), reason="Claude CLI not available")
class SandboxClaudeCliPipelineTest(SandboxAgentPipelineBase):
    agent_type = "cli.claude"
    model = CLAUDE_MODEL


class SandboxCodexSdkPipelineTest(SandboxAgentPipelineBase):
    agent_type = "sdk.codex_sdk"
    llm_provider = "openai"
    model = CODEX_MODEL


class SandboxClaudeSdkPipelineTest(SandboxAgentPipelineBase):
    agent_type = "sdk.claude_sdk"
    model = CLAUDE_MODEL


@pytest.mark.skipif(not sade_available(), reason="SADE not available")
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


class _OnE2b:
    """Run a pipeline class on the e2b backend, with its own session state."""

    sandbox_backend = "e2b"
    session_id = None
    session_dir = None
    env_destroyed = False
    _agent_run_failed = False


class E2bCodexCliPipelineTest(_OnE2b, SandboxCodexCliPipelineTest):
    pass


class E2bClaudeCliPipelineTest(_OnE2b, SandboxClaudeCliPipelineTest):
    pass


class E2bCodexSdkPipelineTest(_OnE2b, SandboxCodexSdkPipelineTest):
    pass


class E2bClaudeSdkPipelineTest(_OnE2b, SandboxClaudeSdkPipelineTest):
    pass


class E2bSadePipelineTest(_OnE2b, SandboxSadePipelineTest):
    pass
