"""Shared skill library helpers for Claude Code and Codex agents."""

from __future__ import annotations

import os
from pathlib import Path

from agent.utils.template import SKILLS_PROMPT_SUFFIX

ENV_ENABLE_SKILLS = "NIKA_ENABLE_SKILLS"

# Default package: src/agent/skills/
_DEFAULT_SKILLS_ROOT = Path(__file__).resolve().parent.parent / "skills"
# Sandbox copies that package to $NIKA_SESSION_DIR/skills (see sbx/workspace.py).
_SANDBOX_SKILLS_DIRNAME = "skills"

CLAUDE_SETTING_SOURCES = ["project"]

_CODEX_AGENTS_MD = """\
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
    # One-shot process override (sandbox / tests) still honored.
    raw = os.getenv(ENV_ENABLE_SKILLS, "").strip().lower()
    if raw:
        return raw not in ("0", "false", "no", "off")
    try:
        from nika.run_config.loader import get_run_config

        return bool(get_run_config().nika.enable_skills)
    except Exception:
        return True


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


def prepare_claude_workspace(workspace: Path) -> None:
    """Link the shared `.claude/` tree into a per-session Claude CLI workspace."""
    package = claude_skills_package_dir()
    if package is None:
        return
    _symlink_or_copy(package / ".claude", workspace / ".claude")


def prepare_codex_workspace(workspace: Path) -> None:
    """Link Codex skills and write a minimal AGENTS.md into the workspace."""
    if not skills_enabled():
        return
    root = resolve_skills_root()
    agents_skills = root / ".agents" / "skills"
    if agents_skills.is_dir():
        _symlink_or_copy(agents_skills, workspace / ".agents" / "skills")
    agents_md = workspace / "AGENTS.md"
    if not agents_md.exists():
        agents_md.write_text(_CODEX_AGENTS_MD, encoding="utf-8")


def diagnosis_prompt_with_skills(base: str) -> str:
    """Append skill guidance to a diagnosis system prompt when skills are enabled."""
    if not skills_enabled():
        return base
    return f"{base.rstrip()}\n\n{SKILLS_PROMPT_SUFFIX}"
