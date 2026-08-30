"""Shared skill library helpers for Claude Code and Codex agents."""

from __future__ import annotations

import os
from pathlib import Path

from agent.utils.template import (
    SKILLS_PROMPT_SUFFIX,
    TEST_SKILLS_PROMPT_SUFFIX,
)


TEST_SKILL_NAME = "nika-test-skill"

# Default package: src/agent/skills/
_DEFAULT_SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"
# Sandbox copies that package to $NIKA_SESSION_DIR/skills (see sbx/workspace.py).
_SANDBOX_SKILLS_DIRNAME = "skills"

CLAUDE_SETTING_SOURCES = ["project"]

_CLAUDE_MD = """\
# NIKA Shared Skill Index

Skills live under `.claude/skills/`. Each directory contains a `SKILL.md` with
YAML frontmatter (`name`, `description`) and workflow instructions.

## Available skills

{skill_rows}

## Authoring

Add a directory under `skills/` with a `SKILL.md` file. Symlinks under
`.claude/skills/` and `.agents/skills/` point at the same source tree.
See `docs/agent-skills.md` for full instructions.
"""

_CODEX_AGENTS_MD = """\
# NIKA Agent Skills

This workspace includes reusable troubleshooting skills under `.agents/skills/`.
Invoke a skill when its description matches the symptoms you observe.
"""

_CODEX_AGENTS_MD_WITH_TEST = """\
# NIKA Agent Skills

This workspace includes reusable troubleshooting skills under `.agents/skills/`.

At the start of every troubleshooting session, invoke `$nika-test-skill` and follow
the marker-first workflow in `.agents/skills/nika-test-skill/SKILL.md` before other
MCP tools.
"""


def resolve_skills_root() -> Path:
    """Return the root directory of the NIKA skill library."""
    if os.environ.get("NIKA_SANDBOX_EXECUTION", "").strip() == "1":
        session_dir = os.environ.get("NIKA_SESSION_DIR", "").strip()
        if session_dir:
            candidate = (
                Path(session_dir).expanduser().resolve() / _SANDBOX_SKILLS_DIRNAME
            )
            if candidate.is_dir():
                return candidate
    return _DEFAULT_SKILLS_ROOT


def skills_enabled() -> bool:
    """Whether agents should load the shared skill library."""
    try:
        from nika.run_config.loader import get_run_config

        return bool(get_run_config().nika.enable_skills)
    except Exception:
        return True


def resolve_test_skill_dir() -> Path | None:
    """Return the test-skill source directory when present."""
    path = resolve_skills_root() / "test_skills" / TEST_SKILL_NAME
    return path if path.is_dir() else None


def claude_skills_package_dir() -> Path | None:
    """Return the directory containing `.claude/` when skills are enabled."""
    if not skills_enabled():
        return None
    root = resolve_skills_root()
    if (root / ".claude").is_dir():
        return root
    return None


def _symlink_or_copy(src: Path, dest: Path) -> None:
    if dest.exists() or dest.is_symlink():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.symlink_to(src, target_is_directory=src.is_dir())
    except OSError:
        if src.is_dir():
            import shutil

            shutil.copytree(src, dest)
        else:
            import shutil

            shutil.copy2(src, dest)


def _iter_production_skills(root: Path) -> list[Path]:
    skills = root / "skills"
    if not skills.is_dir():
        return []
    return sorted(
        p for p in skills.iterdir() if p.is_dir() and not p.name.startswith(".")
    )


def _skill_table_rows(skill_names: list[str]) -> str:
    if not skill_names:
        return "| _(none yet)_ | Add skills under `skills/` |"
    rows: list[str] = []
    for name in skill_names:
        if name == TEST_SKILL_NAME:
            rows.append(
                f"| `{name}` | Integration-test only — invoke at the start of "
                "every session to confirm skill loading |"
            )
        else:
            rows.append(f"| `{name}` | See `.claude/skills/{name}/SKILL.md` |")
    return "\n".join(rows)


def _link_skills(skills_dst: Path, *, with_test: bool) -> list[str]:
    root = resolve_skills_root()
    if skills_dst.is_symlink():
        skills_dst.unlink()
    skills_dst.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for skill in _iter_production_skills(root):
        _symlink_or_copy(skill, skills_dst / skill.name)
        names.append(skill.name)
    if with_test:
        src = resolve_test_skill_dir()
        if src is not None:
            _symlink_or_copy(src, skills_dst / TEST_SKILL_NAME)
            if TEST_SKILL_NAME not in names:
                names.append(TEST_SKILL_NAME)
    return names


def _materialize_claude_tree(dest: Path, *, with_test: bool) -> None:
    """Write ``dest/.claude/`` with production skills and optional test skill."""
    claude_dst = dest / ".claude"
    if claude_dst.is_symlink():
        claude_dst.unlink()
    names = _link_skills(claude_dst / "skills", with_test=with_test)
    claude_md = claude_dst / "CLAUDE.md"
    if with_test or not claude_md.exists():
        claude_md.write_text(
            _CLAUDE_MD.format(skill_rows=_skill_table_rows(names)),
            encoding="utf-8",
        )
    else:
        src_md = resolve_skills_root() / ".claude" / "CLAUDE.md"
        if src_md.is_file():
            _symlink_or_copy(src_md, claude_md)


def prepare_claude_workspace(
    workspace: Path, *, include_test_skill: bool = False
) -> None:
    """Materialize `.claude/` into a per-session Claude CLI workspace.

    Pass ``include_test_skill=True`` from tests to also link ``nika-test-skill``.
    """
    if not skills_enabled():
        return
    root = resolve_skills_root()
    if not (root / ".claude").is_dir() and not (root / "skills").is_dir():
        return
    if include_test_skill:
        _materialize_claude_tree(workspace, with_test=True)
        return
    package = claude_skills_package_dir()
    if package is None:
        return
    _symlink_or_copy(package / ".claude", workspace / ".claude")


def prepare_codex_workspace(
    workspace: Path, *, include_test_skill: bool = False
) -> None:
    """Link Codex skills and write a minimal AGENTS.md into the workspace.

    Pass ``include_test_skill=True`` from tests to also link ``nika-test-skill``.
    """
    if not skills_enabled():
        return
    _link_skills(workspace / ".agents" / "skills", with_test=include_test_skill)
    agents_md = workspace / "AGENTS.md"
    if not agents_md.exists():
        text = _CODEX_AGENTS_MD_WITH_TEST if include_test_skill else _CODEX_AGENTS_MD
        agents_md.write_text(text, encoding="utf-8")


def diagnosis_prompt_with_skills(base: str, *, include_test_skill: bool = False) -> str:
    """Append skill guidance to a diagnosis system prompt when skills are enabled.

    Pass ``include_test_skill=True`` from tests to require the marker-first workflow.
    """
    if not skills_enabled():
        return base
    suffix = TEST_SKILLS_PROMPT_SUFFIX if include_test_skill else SKILLS_PROMPT_SUFFIX
    return f"{base.rstrip()}\n\n{suffix}"
