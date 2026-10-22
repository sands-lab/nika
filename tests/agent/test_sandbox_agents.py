from __future__ import annotations

import functools
import json
import re
import subprocess

import pytest
from agent.protocols import DIAGNOSIS
from nika.agents.registry import get_agent_spec
from nika.agents.sandbox.sbx.policy import sanitize_sandbox_name
from nika.run_config.loader import (
    load_run_config,
    reset_run_config,
    set_run_config,
)
from nika.run_config.schema import RunConfig
from nika.utils.agent_session_id import resolve_agent_session_id
from nika.utils.session_store import SessionStore
from tests.agent._assertions import (
    assert_phase_messages,
    assert_no_cli_native_mirrors,
    assert_no_codex_tool_item_mirrors,
)
from tests.agent.sandbox_support import (
    sandbox_backend_available,
    sandbox_provider_credential_available,
)
from tests.support.integration_base import OrderedPipelineTestCase
from tests.support.integration_pipeline import (
    CommonPipelineSteps,
    live_anthropic_test_config,
    live_custom_base_url,
    live_openai_test_config,
    load_test_env,
)

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox, pytest.mark.live]

load_test_env()
CLAUDE_PROVIDER, CLAUDE_MODEL = live_anthropic_test_config()
# Codex and BYO agents: OpenAI API keys, a ChatGPT login stored in sbx, or a
# compatible custom endpoint.
OPENAI_PROVIDER, OPENAI_MODEL = live_openai_test_config()
MAX_STEPS = 20
_backend_available = functools.cache(sandbox_backend_available)
_SENSITIVE_NAMES = ("auth.json", ".credentials.json", ".host_auth")
# Nothing the agent sees may name the benchmark that runs it.
_HOST_NAME = re.compile(rb"nika", re.IGNORECASE)


def _isolate_agent_run_config(
    *, agent_type: str, provider: str, model: str, backend: str, base_url: str
) -> None:
    """Avoid host config/nika.yaml base_url leaking into provider env.

    Keeps the host's E2B service address (``nika.sandbox.url``), so the E2B
    variants run against the configured service, such as AgentENV.
    """
    reset_run_config()
    sandbox: dict[str, object] = {
        "backend": backend,
        "offline_sdk_wheels": True,
        # Inspected after the run; session close removes it.
        "keep": backend == "sbx",
    }
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
                    "base_url": base_url if provider == "custom" else None,
                },
            }
        )
    )


def _host_name_mentions(texts: list[bytes]) -> list[str]:
    """Distinct contexts of the places *texts* name the benchmark."""
    return sorted(
        {
            text[max(match.start() - 80, 0) : match.end() + 80].decode(errors="replace")
            for text in texts
            for match in _HOST_NAME.finditer(text)
        }
    )


class SandboxAgentPipelineBase(CommonPipelineSteps, OrderedPipelineTestCase):
    agent_type: str = ""
    sandbox_backend: str = "sbx"
    llm_provider: str = CLAUDE_PROVIDER
    model: str = CLAUDE_MODEL
    _agent_run_failed: bool = False
    # Request bodies the agent sent to a custom endpoint.

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
            base_url=live_custom_base_url(),
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
            self._run_sandbox_agent()
            row = SessionStore().get_session(self.session_id)
            assert row.get("agent_type") == self.agent_type
        except FileNotFoundError as exc:
            type(self)._agent_run_failed = True
            pytest.skip(f"Session store missing during agent run: {exc}")

    def _run_sandbox_agent(self) -> None:
        self._run_agent(
            agent_type=self.agent_type,
            llm_provider=self.llm_provider,
            model=self.model,
            max_steps=MAX_STEPS,
            reasoning_effort="low" if "codex" in self.agent_type else None,
        )

    def _assert_sandbox_artifacts(self) -> None:
        assert self.session_dir is not None
        manifest = self.session_dir / "sandbox_manifest.json"
        assert manifest.is_file()
        data = json.loads(manifest.read_text(encoding="utf-8"))
        assert data["agent_class"] == get_agent_spec(self.agent_type).class_path
        text = manifest.read_text(encoding="utf-8")
        assert not _host_name_mentions([text.encode()])
        assert "OPENAI_API_KEY" not in text
        assert "ANTHROPIC_API_KEY" not in text
        assert not (self.session_dir / ".sandbox_run").exists()
        for dirname in (
            "codex_workspace",
            "claude_workspace",
            "opencode_workspace",
            "dsh_workspace",
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
        assert_no_cli_native_mirrors(messages)
        phases = {e["phase"] for e in messages}
        assert DIAGNOSIS in phases

    def test_step_05_agent_view_names_no_host(self) -> None:
        """Neither the model traffic nor the sandbox names the benchmark."""
        if type(self)._agent_run_failed:
            pytest.skip("Skipping the agent view check due to agent-run failure")
        # The host's record of every model request, as the agent sent it.
        requests = [
            json.dumps(call["request"]).encode()
            for call in self._load_jsonl("model_calls.jsonl")
        ]
        assert requests, "the agent sent no model request"
        assert _host_name_mentions(requests) == []
        if self.sandbox_backend != "sbx":
            return
        row = SessionStore().get_session(self.session_id)
        sandbox = sanitize_sandbox_name(resolve_agent_session_id(row))
        probe = subprocess.run(
            [
                "sbx",
                "exec",
                sandbox,
                "bash",
                "-lc",
                "hostname; pwd; env; ls -a ~ /tmp; cat /etc/hosts; pip3 list",
            ],
            capture_output=True,
            check=False,
            timeout=120,
        )
        assert probe.returncode == 0, probe.stderr
        assert _host_name_mentions([probe.stdout]) == []
        # The agent reaches only the MCP gateway and the model proxy: not the
        # benchmark's public source, package indexes, or model APIs.
        egress = subprocess.run(
            [
                "sbx",
                "exec",
                sandbox,
                "bash",
                "-lc",
                "for url in https://github.com https://pypi.org/simple/ "
                "https://api.anthropic.com; do "
                "curl -s -o /dev/null -w '%{http_code} ' -m 10 $url; done",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert egress.stdout.split() == ["403", "403", "403"], egress.stdout

    def test_step_06_session_close(self) -> None:
        if type(self)._agent_run_failed:
            pytest.skip("Skipping session close verification due to agent-run failure")
        self._step_close_and_verify(self.agent_type)


class SandboxCodexCliPipelineTest(SandboxAgentPipelineBase):
    agent_type = "cli.codex"
    llm_provider = OPENAI_PROVIDER
    model = OPENAI_MODEL


class SandboxClaudeCliPipelineTest(SandboxAgentPipelineBase):
    agent_type = "cli.claude"
    model = CLAUDE_MODEL


class SandboxOpenCodeCliPipelineTest(SandboxAgentPipelineBase):
    agent_type = "cli.opencode"
    llm_provider = OPENAI_PROVIDER
    model = OPENAI_MODEL


class SandboxDshCliPipelineTest(SandboxAgentPipelineBase):
    agent_type = "cli.dsh"
    llm_provider = OPENAI_PROVIDER
    model = OPENAI_MODEL


class SandboxCodexSdkPipelineTest(SandboxAgentPipelineBase):
    agent_type = "sdk.codex_sdk"
    llm_provider = OPENAI_PROVIDER
    model = OPENAI_MODEL


class SandboxLangGraphPipelineTest(SandboxAgentPipelineBase):
    agent_type = "byo.langgraph"
    llm_provider = OPENAI_PROVIDER
    model = OPENAI_MODEL


class SandboxMcpAgentPipelineTest(SandboxAgentPipelineBase):
    agent_type = "byo.mcp_agent"
    llm_provider = OPENAI_PROVIDER
    model = OPENAI_MODEL


class SandboxAutogenPipelineTest(SandboxAgentPipelineBase):
    agent_type = "byo.autogen"
    llm_provider = OPENAI_PROVIDER
    model = OPENAI_MODEL


class SandboxClaudeSdkPipelineTest(SandboxAgentPipelineBase):
    agent_type = "sdk.claude_sdk"
    model = CLAUDE_MODEL


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


class E2bOpenCodeCliPipelineTest(_OnE2b, SandboxOpenCodeCliPipelineTest):
    pass


class E2bDshCliPipelineTest(_OnE2b, SandboxDshCliPipelineTest):
    pass


class E2bCodexSdkPipelineTest(_OnE2b, SandboxCodexSdkPipelineTest):
    pass


class E2bClaudeSdkPipelineTest(_OnE2b, SandboxClaudeSdkPipelineTest):
    pass


class E2bSadePipelineTest(_OnE2b, SandboxSadePipelineTest):
    pass


class E2bLangGraphPipelineTest(_OnE2b, SandboxLangGraphPipelineTest):
    pass


class E2bMcpAgentPipelineTest(_OnE2b, SandboxMcpAgentPipelineTest):
    pass


class E2bAutogenPipelineTest(_OnE2b, SandboxAutogenPipelineTest):
    pass
