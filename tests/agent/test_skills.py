from __future__ import annotations

import pytest
import os
import tempfile
import unittest.mock
from pathlib import Path
from agent.utils.skills import (
    ENV_ENABLE_SKILLS,
    claude_skills_package_dir,
    diagnosis_prompt_with_skills,
    prepare_claude_workspace,
    prepare_codex_workspace,
    resolve_skills_root,
    skills_enabled,
)
from agent.utils.template import OVERALL_DIAGNOSIS_PROMPT, SKILLS_PROMPT_SUFFIX
from nika.utils.agent_config import ENV_CODEX_MODEL, ENV_CODEX_SDK_MODEL
from nika.utils.session_store import SessionStore
from tests.agent._assertions import (
    assert_skill_invoked,
    assert_submission_fields,
    marker_before_first_mcp_tool,
    skill_invoked,
)
from tests.agent.sandbox_support import SANDBOX_E2E_SUPERSEDED
from tests.support.integration_base import OrderedPipelineTestCase
from tests.support.integration_pipeline import (
    ClabCommonPipelineSteps,
    CommonPipelineSteps,
    _min3clos_prerequisites,
    claude_cli_available,
    claude_sdk_available,
    codex_cli_available,
    codex_sdk_available,
    load_test_env,
)

load_test_env()
CODEX_MODEL = (
    os.environ.get(ENV_CODEX_SDK_MODEL, "").strip()
    or os.environ.get(ENV_CODEX_MODEL, "").strip()
    or "gpt-5.4-mini"
)


class SkillsConfigTest:
    def test_resolve_skills_root_default(self) -> None:
        root = resolve_skills_root()
        assert (root / "skills" / "nika-test-skill" / "SKILL.md").is_file()

    def test_resolve_skills_root_sandbox_session_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            skills_root = session_dir / "skills"
            skills_root.mkdir()
            with unittest.mock.patch.dict(
                os.environ,
                {
                    "NIKA_SANDBOX_EXECUTION": "1",
                    "NIKA_SESSION_DIR": str(session_dir),
                },
                clear=False,
            ):
                assert resolve_skills_root() == skills_root.resolve()

    def test_skills_enabled_default(self) -> None:
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            assert skills_enabled()

    def test_skills_enabled_false(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {ENV_ENABLE_SKILLS: "false"}, clear=True
        ):
            assert not skills_enabled()

    def test_claude_skills_package_dir_when_disabled(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {ENV_ENABLE_SKILLS: "false"}, clear=True
        ):
            assert claude_skills_package_dir() is None

    def test_claude_skills_package_dir_when_enabled(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {ENV_ENABLE_SKILLS: "true"}, clear=True
        ):
            package = claude_skills_package_dir()
            assert package is not None
            assert (package / ".claude" / "CLAUDE.md").is_file()

    def test_diagnosis_prompt_with_skills(self) -> None:
        with unittest.mock.patch.dict(
            os.environ, {ENV_ENABLE_SKILLS: "true"}, clear=True
        ):
            prompt = diagnosis_prompt_with_skills(OVERALL_DIAGNOSIS_PROMPT)
            assert SKILLS_PROMPT_SUFFIX in prompt
        with unittest.mock.patch.dict(
            os.environ, {ENV_ENABLE_SKILLS: "false"}, clear=True
        ):
            assert (
                diagnosis_prompt_with_skills(OVERALL_DIAGNOSIS_PROMPT)
                == OVERALL_DIAGNOSIS_PROMPT
            )


class SkillsWorkspaceTest:
    def test_prepare_claude_workspace_links_dot_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with unittest.mock.patch.dict(
                os.environ, {ENV_ENABLE_SKILLS: "true"}, clear=True
            ):
                prepare_claude_workspace(workspace)
            link = workspace / ".claude"
            assert link.exists()
            assert (link / "skills" / "nika-test-skill" / "SKILL.md").exists()

    def test_prepare_codex_workspace_links_agents_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with unittest.mock.patch.dict(
                os.environ, {ENV_ENABLE_SKILLS: "true"}, clear=True
            ):
                prepare_codex_workspace(workspace)
            assert (
                workspace / ".agents" / "skills" / "nika-test-skill" / "SKILL.md"
            ).exists()
            assert (workspace / "AGENTS.md").is_file()


class SkillAssertionTest:
    def test_skill_invoked_from_tool_start(self) -> None:
        messages = [
            {
                "event": "tool_start",
                "tool": {"name": "Skill"},
                "input": "{'skill': 'nika-test-skill'}",
            }
        ]
        assert skill_invoked(messages)

    def test_skill_invoked_from_claude_event(self) -> None:
        messages = [
            {
                "event": "assistant",
                "claude_event": {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Skill",
                                "input": {"skill": "nika-test-skill"},
                            }
                        ]
                    },
                },
            }
        ]
        assert skill_invoked(messages)

    def test_marker_before_first_mcp_tool(self) -> None:
        messages = [
            {"event": "llm_end", "text": "NIKA_TEST_SKILL_ACTIVE"},
            {
                "event": "tool_start",
                "tool": {"name": "get_reachability"},
                "input": "{}",
            },
        ]
        assert marker_before_first_mcp_tool(messages)

    def test_marker_must_precede_mcp_tools(self) -> None:
        messages = [
            {
                "event": "tool_start",
                "tool": {"name": "get_reachability"},
                "input": "{}",
            },
            {"event": "llm_end", "text": "NIKA_TEST_SKILL_ACTIVE"},
        ]
        assert not marker_before_first_mcp_tool(messages)


def _skills_env_patch() -> dict[str, str]:
    return {ENV_ENABLE_SKILLS: "true"}


class _SkillPipelineMixin:
    agent_id: str
    agent_model: str | None = None
    max_steps: str = "20"

    def _agent_run_kwargs(self) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "agent_type": self.agent_id,
            "max_steps": int(self.max_steps),
        }
        if self.agent_model:
            kwargs["model"] = self.agent_model
        return kwargs

    def test_step_03_run_agent_with_skills(self) -> None:
        assert self.session_id is not None
        with unittest.mock.patch.dict(os.environ, _skills_env_patch(), clear=False):
            self._run_agent(**self._agent_run_kwargs())
        row = SessionStore().get_session(self.session_id)
        assert row.get("agent_type") == self.agent_id

    def test_step_04_check_skill_invocation(self) -> None:
        assert self.session_dir is not None
        messages = self._load_jsonl("messages.jsonl")
        assert_skill_invoked(messages)

    def test_step_05_check_submission(self) -> None:
        assert self.session_dir is not None
        assert (self.session_dir / "submission.json").exists()
        assert_submission_fields(self.session_dir)


@SANDBOX_E2E_SUPERSEDED
@pytest.mark.skipif(
    not claude_sdk_available(),
    reason="claude-agent-sdk + ANTHROPIC credentials required",
)
class ClaudeSdkSkillPipelineTest(
    _SkillPipelineMixin, CommonPipelineSteps, OrderedPipelineTestCase
):
    agent_id = "sdk.claude_sdk"

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_06_session_close(self) -> None:
        self._step_close_and_verify(self.agent_id)


@SANDBOX_E2E_SUPERSEDED
@pytest.mark.skipif(
    not claude_cli_available(), reason="Claude CLI + ANTHROPIC credentials required"
)
class ClaudeCliSkillPipelineTest(
    _SkillPipelineMixin, CommonPipelineSteps, OrderedPipelineTestCase
):
    agent_id = "cli.claude"

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_06_session_close(self) -> None:
        self._step_close_and_verify(self.agent_id)


@SANDBOX_E2E_SUPERSEDED
@pytest.mark.skipif(
    not codex_cli_available(), reason="Codex CLI and OpenAI credentials required"
)
class CodexCliSkillPipelineTest(
    _SkillPipelineMixin, CommonPipelineSteps, OrderedPipelineTestCase
):
    agent_id = "cli.codex"
    agent_model = CODEX_MODEL

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_06_session_close(self) -> None:
        self._step_close_and_verify(self.agent_id)


@SANDBOX_E2E_SUPERSEDED
@pytest.mark.skipif(
    not codex_sdk_available(), reason="openai-codex + ~/.codex/auth.json required"
)
class CodexSdkSkillPipelineTest(
    _SkillPipelineMixin, CommonPipelineSteps, OrderedPipelineTestCase
):
    agent_id = "sdk.codex_sdk"
    agent_model = CODEX_MODEL

    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()

    def test_step_06_session_close(self) -> None:
        self._step_close_and_verify(self.agent_id)


class _ClabSkillPipelineMixin(_SkillPipelineMixin):
    def test_step_01_start_env(self) -> None:
        self._step_start_env()

    def test_step_02_inject_failure(self) -> None:
        self._step_inject_failure()


@SANDBOX_E2E_SUPERSEDED
@pytest.mark.skipif(
    not (_min3clos_prerequisites() and claude_sdk_available()),
    reason="containerlab/gnmic/Docker or claude-agent-sdk credentials required",
)
class ClaudeSdkClabSkillPipelineTest(
    _ClabSkillPipelineMixin, ClabCommonPipelineSteps, OrderedPipelineTestCase
):
    agent_id = "sdk.claude_sdk"

    def test_step_06_session_close(self) -> None:
        self._step_close_and_verify(self.agent_id)


@SANDBOX_E2E_SUPERSEDED
@pytest.mark.skipif(
    not (_min3clos_prerequisites() and claude_cli_available()),
    reason="containerlab/gnmic/Docker or Claude CLI credentials required",
)
class ClaudeCliClabSkillPipelineTest(
    _ClabSkillPipelineMixin, ClabCommonPipelineSteps, OrderedPipelineTestCase
):
    agent_id = "cli.claude"

    def test_step_06_session_close(self) -> None:
        self._step_close_and_verify(self.agent_id)


@SANDBOX_E2E_SUPERSEDED
@pytest.mark.skipif(
    not (_min3clos_prerequisites() and codex_cli_available()),
    reason="containerlab/gnmic/Docker or Codex CLI credentials required",
)
class CodexCliClabSkillPipelineTest(
    _ClabSkillPipelineMixin, ClabCommonPipelineSteps, OrderedPipelineTestCase
):
    agent_id = "cli.codex"
    agent_model = CODEX_MODEL

    def test_step_06_session_close(self) -> None:
        self._step_close_and_verify(self.agent_id)


@SANDBOX_E2E_SUPERSEDED
@pytest.mark.skipif(
    not (_min3clos_prerequisites() and codex_sdk_available()),
    reason="containerlab/gnmic/Docker or openai-codex credentials required",
)
class CodexSdkClabSkillPipelineTest(
    _ClabSkillPipelineMixin, ClabCommonPipelineSteps, OrderedPipelineTestCase
):
    agent_id = "sdk.codex_sdk"
    agent_model = CODEX_MODEL

    def test_step_06_session_close(self) -> None:
        self._step_close_and_verify(self.agent_id)
