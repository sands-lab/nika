# Agent Skills

NIKA supports reusable **agent skills** for Claude Code and Codex agents. A skill is a directory with a `SKILL.md` file that teaches the agent a structured troubleshooting workflow.

Skills are optional. Enable or disable them with `NIKA_ENABLE_SKILLS` (default: `true`).

## Supported agents

| Agent | Skill mechanism |
|-------|-----------------|
| `sdk.claude_sdk` | Claude Code `Skill` tool + `.claude/skills/` |
| `cli.claude` | Same as above via `claude -p --setting-sources project` |
| `cli.codex` | Codex skills under `.agents/skills/` + `AGENTS.md` |
| `sdk.codex_sdk` | Same as Codex CLI |
| `community.sade` | Own 15-skill library under `src/agent/community/sade/.claude/` (separate from the shared library) |

`byo.langgraph`, `byo.mcp_agent`, and `byo.autogen` do not load skills.

## Default skill library

The shared library lives at [`src/agent/skills/`](../src/agent/skills/):

```
src/agent/skills/
├── skills/                 # canonical skill sources
│   └── nika-test-skill/
├── .claude/
│   ├── CLAUDE.md           # skill index for Claude Code
│   └── skills/ → ../skills/
└── .agents/
    └── skills/ → ../skills/   # Codex discovery path
```

Claude agents load `.claude/` via `setting_sources=["project"]`. Codex agents receive `.agents/skills/` and a short `AGENTS.md` in the per-session workspace.

## Configuration

| Env | Default | Purpose |
|-----|---------|---------|
| `NIKA_ENABLE_SKILLS` | `true` | Set to `false` to disable skill loading |
| `NIKA_SKILLS_DIR` | `src/agent/skills/` | Override the skill library root |

When skills are enabled, the diagnosis system prompt includes a short suffix describing how to invoke skills (`agent.utils.template.SKILLS_PROMPT_SUFFIX`).

## Writing a custom skill

### 1. Create `SKILL.md`

Every skill needs YAML frontmatter and markdown instructions:

```markdown
---
name: my-link-skill
description: Diagnose link and interface faults. Use when an interface is DOWN or flapping.
---

# Link Faults

1. Call `get_host_net_config` on the suspect host.
2. Call `exec_shell` with `ip link show`.
3. ...
```

**Description tips**

- Write in third person (the description is used for discovery).
- Include both **what** the skill does and **when** to use it.
- Keep `SKILL.md` under ~500 lines; put long references in sibling files.

### 2. Add the skill directory

Place the skill under the canonical tree:

```text
src/agent/skills/skills/my-link-skill/SKILL.md
```

Symlinks under `.claude/skills/` and `.agents/skills/` already point at `skills/`, so new directories are picked up automatically.

### 3. Register in `CLAUDE.md` (recommended)

Add a row to [`src/agent/skills/.claude/CLAUDE.md`](../src/agent/skills/.claude/CLAUDE.md) so Claude Code agents can route symptoms to your skill.

### 4. Optional helper scripts

Add scripts under `skills/my-link-skill/scripts/` and document how to run them. SADE uses `h.py` as a stable launcher; shared skills can reference MCP tools directly or add a similar launcher if needed.

### 5. Test locally

Run a small scenario with skills enabled:

```shell
export NIKA_ENABLE_SKILLS=true
uv run nika env run simple_bgp
uv run nika failure inject link_down --set host_name=pc1 --set intf_name=eth0
uv run nika agent run -a sdk.claude_sdk -n 20
```

Inspect `results/{session_id}/messages.jsonl` for `Skill` tool calls (Claude) or skill-name mentions (Codex).

Unit and integration tests: `tests/agent/test_skills.py`.

Integration coverage (real `simple_bgp` / `link_down` scenario):

| Test class | Agent |
|------------|-------|
| `ClaudeSdkSkillPipelineTest` | `sdk.claude_sdk` |
| `ClaudeCliSkillPipelineTest` | `cli.claude` |
| `CodexCliSkillPipelineTest` | `cli.codex` |
| `CodexSdkSkillPipelineTest` | `sdk.codex_sdk` |

Claude CLI loads project skills only when **not** using `--bare` (skills enabled disables bare mode so the Skill tool is available).

## Claude vs Codex invocation

**Claude Code agents** use the native Skill tool:

```text
Skill(skill="my-link-skill")
```

The parameter name is `skill`, not `name`.

**Codex agents** discover skills from `.agents/skills/` and can be invoked with:

```text
$my-link-skill
```

Or implicitly when the task matches the skill description.

## Advanced example: SADE

[`community.sade`](../src/agent/community/sade/README.md) ships a 15-skill fault-family library with phase-gated prompts, `CLAUDE.md` routing, and the `h.py` helper launcher. It uses the same Claude Code mechanism but keeps its own `.claude/` tree under the SADE package directory.

Use SADE as a reference for large skill libraries; use `src/agent/skills/` for shared or project-specific additions.

## Implementation reference

- Shared helpers: [`src/agent/utils/skills.py`](../src/agent/utils/skills.py)
- Prompt suffix: [`src/agent/utils/template.py`](../src/agent/utils/template.py)
- Test skill: [`src/agent/skills/skills/nika-test-skill/SKILL.md`](../src/agent/skills/skills/nika-test-skill/SKILL.md)
