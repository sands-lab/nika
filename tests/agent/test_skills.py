from __future__ import annotations

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
from tests.agent._assertions import marker_before_first_mcp_tool, skill_invoked
from tests.support.integration_pipeline import load_test_env

load_test_env()


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
