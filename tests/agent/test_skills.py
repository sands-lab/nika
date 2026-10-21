from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent.utils.skills import (
    TEST_SKILL_NAME,
    claude_skills_package_dir,
    diagnosis_prompt_with_skills,
    prepare_claude_workspace,
    prepare_codex_workspace,
    resolve_skills_root,
    resolve_test_skill_dir,
    skills_enabled,
)
from agent.utils.template import (
    OVERALL_DIAGNOSIS_PROMPT,
    SKILLS_PROMPT_SUFFIX,
    TEST_SKILLS_PROMPT_SUFFIX,
)
from tests.agent._assertions import marker_before_first_mcp_tool, skill_invoked

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def set_skills(agent_task):
    """Write the task with skills on (the run config default) or off."""

    def write(enabled: bool) -> Path:
        return agent_task(enable_skills=enabled)

    write(True)
    return write


class SkillsConfigTest:
    def test_resolve_skills_root_default(self) -> None:
        root = resolve_skills_root()
        assert not (root / "test_skills").exists()
        assert not (root / "skills" / TEST_SKILL_NAME).exists()
        test_dir = resolve_test_skill_dir()
        assert test_dir is not None
        assert (test_dir / "SKILL.md").is_file()
        assert "tests" in test_dir.parts

    def test_resolve_skills_root_workspace_copy(self, set_skills) -> None:
        workspace = set_skills(True)
        skills_root = workspace / "skills"
        skills_root.mkdir()
        assert resolve_skills_root() == skills_root.resolve()

    def test_skills_enabled_default(self) -> None:
        assert skills_enabled()

    def test_skills_enabled_false(self, set_skills) -> None:
        set_skills(False)
        assert not skills_enabled()

    def test_claude_skills_package_dir_when_disabled(self, set_skills) -> None:
        set_skills(False)
        assert claude_skills_package_dir() is None

    def test_claude_skills_package_dir_when_enabled(self) -> None:
        package = claude_skills_package_dir()
        assert package is not None
        assert (package / ".claude" / "CLAUDE.md").is_file()
        assert not (
            package / ".claude" / "skills" / TEST_SKILL_NAME / "SKILL.md"
        ).exists()

    def test_diagnosis_prompt_with_skills(self, set_skills) -> None:
        prompt = diagnosis_prompt_with_skills(OVERALL_DIAGNOSIS_PROMPT)
        assert SKILLS_PROMPT_SUFFIX in prompt
        assert TEST_SKILL_NAME not in prompt
        test_prompt = diagnosis_prompt_with_skills(
            OVERALL_DIAGNOSIS_PROMPT, include_test_skill=True
        )
        assert TEST_SKILLS_PROMPT_SUFFIX in test_prompt
        set_skills(False)
        assert (
            diagnosis_prompt_with_skills(OVERALL_DIAGNOSIS_PROMPT)
            == OVERALL_DIAGNOSIS_PROMPT
        )


class SkillsWorkspaceTest:
    def test_prepare_claude_workspace_excludes_test_skill_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            prepare_claude_workspace(workspace)
            link = workspace / ".claude"
            assert link.exists()
            assert not (link / "skills" / TEST_SKILL_NAME).exists()

    def test_prepare_claude_workspace_includes_test_skill_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            prepare_claude_workspace(workspace, include_test_skill=True)
            assert (
                workspace / ".claude" / "skills" / TEST_SKILL_NAME / "SKILL.md"
            ).exists()

    def test_prepare_codex_workspace_excludes_test_skill_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            prepare_codex_workspace(workspace)
            assert not (workspace / ".agents" / "skills" / TEST_SKILL_NAME).exists()
            assert (workspace / "AGENTS.md").is_file()
            assert TEST_SKILL_NAME not in (workspace / "AGENTS.md").read_text(
                encoding="utf-8"
            )

    def test_prepare_codex_workspace_includes_test_skill_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            prepare_codex_workspace(workspace, include_test_skill=True)
            assert (
                workspace / ".agents" / "skills" / TEST_SKILL_NAME / "SKILL.md"
            ).exists()
            assert TEST_SKILL_NAME in (workspace / "AGENTS.md").read_text(
                encoding="utf-8"
            )


class SkillAssertionTest:
    def test_skill_invoked_from_tool_start(self) -> None:
        messages = [
            {
                "event": "tool_start",
                "tool": {"name": "Skill"},
                "input": "{'skill': 'skill-loading-check'}",
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
                                "input": {"skill": "skill-loading-check"},
                            }
                        ]
                    },
                },
            }
        ]
        assert skill_invoked(messages)

    def test_marker_before_first_mcp_tool(self) -> None:
        messages = [
            {"event": "llm_end", "text": "SKILL_LOADING_CHECK_ACTIVE"},
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
            {"event": "llm_end", "text": "SKILL_LOADING_CHECK_ACTIVE"},
        ]
        assert not marker_before_first_mcp_tool(messages)
