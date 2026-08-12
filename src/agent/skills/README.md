# NIKA Shared Agent Skills

Default skill library for Claude Code and Codex agents (`nika.enable_skills: true` in `config/nika.yaml`).

```
skills/                 # canonical skill sources (loaded by default)
test_skills/            # integration-only (loaded via include_test_skill=True)
.claude/skills/         # → skills/ (Claude Code)
.agents/skills/         # → skills/ (Codex)
```

See [docs/agent-skills.md](../../../docs/agent-skills.md).
