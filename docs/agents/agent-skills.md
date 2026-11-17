# Give an agent skills

A **skill** is a set of reusable troubleshooting instructions that an agent can load during diagnosis. Use one when several incidents share a failure pattern and you want the agent to run the same checks each time. A skill is a directory that contains a `SKILL.md` file.

Skills are on by default for the agents that support them. Set `agent.enable_skills` in `config/nika.yaml` to `false` to turn them off.

## Which agents load skills

| Agent | Skill source |
| --- | --- |
| `sdk.claude_sdk`, `cli.claude` | The shared library, through Claude Code's skill discovery |
| `cli.codex`, `sdk.codex_sdk` | The shared library, through Codex skill discovery |
| `cli.opencode` | The shared library |
| `cli.dsh` | The shared library |
| `community.sade` | Its own 15-skill library, separate from the shared library |

`byo.langgraph`, `byo.mcp_agent`, and `byo.autogen` do not load skills.

## Write a skill

### 1. Create `SKILL.md`

Every skill needs YAML frontmatter and markdown instructions:

```markdown
---
name: my-link-skill
description: Diagnose link and interface failures. Use when an interface is DOWN or flapping.
---

# Link Faults

1. Call `exec_shell` on the suspect host with `ip addr && ip route`.
2. Call `exec_shell` with `ip link show`.
3. ...
```

Write the description for discovery. It should say what the skill does and when to use it. Keep `SKILL.md` under about 500 lines, and move long reference material into sibling files.

### 2. Add the skill to the shared library

Place the skill directory under the library's skill folder:

```text
src/agent/skills/skills/my-link-skill/SKILL.md
```

The shared library in the repository currently contains no skills. An agent with `enable_skills` on loads whatever you add here.

### 3. Route to it from the index (recommended)

Add a row to [`src/agent/skills/.claude/CLAUDE.md`](../../src/agent/skills/.claude/CLAUDE.md). Claude Code agents use this index to route symptoms to your skill.

### 4. Add helper scripts (optional)

Put scripts under `skills/my-link-skill/scripts/`, and document how to run them in `SKILL.md`. SADE uses `h.py` as a stable launcher. Shared skills can call MCP tools directly, or add a similar launcher.

### 5. Try it on one environment

Deploy a small environment with skills on, inject a failure, and run an agent:

```shell
uv run nika env run dc_clos -s s
uv run nika failure inject link_down --set host_name=client_1_1 --set intf_name=eth0
uv run nika agent run -a sdk.claude_sdk -p deepseek -m deepseek-v4-flash
```

Then check `results/{session_id}/messages.jsonl` for the skill calls: `Skill` tool calls for Claude, `$skill` mentions for Codex, and `skill` tool calls for OpenCode and DeepSeek Harness.

---

The rest of this page is reference: how agents invoke skills, and where the code lives.

## How agents invoke skills

Claude Code agents use the native `Skill` tool, with the parameter `skill`:

```text
Skill(skill="my-link-skill")
```

Codex agents discover skills in `.agents/skills/`. They invoke one by name, such as `$my-link-skill`, or when a task matches its description. OpenCode and DeepSeek Harness agents call:

```text
skill({ name: "my-link-skill" })
```

Codex and DeepSeek Harness read a short `AGENTS.md` that NIKA writes into the session workspace, along with the skill directories. Claude Code reads the `.claude/` tree of the library.

## Advanced example: SADE

[`community.sade`](community/sade.md) ships a 15-skill failure-family library, with phase-gated prompts, `CLAUDE.md` routing, and the `h.py` helper launcher. It uses the same Claude Code mechanism, but keeps its own `.claude/` tree under the SADE package. Use SADE as a model for a large library, and use `src/agent/skills/` for shared or project-specific additions.

## Test skills

Run the skill unit and integration tests with:

```shell
uv run pytest tests/agent/test_skills.py -v
```

The test skill `skill-loading-check` is a fixture under [`tests/agent/fixtures/skills/`](../../tests/agent/fixtures/skills/skill-loading-check/SKILL.md). It is not part of the skill library. [Testing](../development/testing.md) describes how unit and live sandbox tests load it.

## Code reference

- Shared helpers: [`src/agent/utils/skills.py`](../../src/agent/utils/skills.py)
- Shared library layout: [`src/agent/skills/`](../../src/agent/skills/)
