---
name: nika-test-skill
description: Integration-test skill only. Invoke at the start of every troubleshooting session before other diagnostics to confirm the NIKA skill library is loaded.
---

# NIKA Test Skill

This skill exists only to verify NIKA skill loading in automated tests. It does not replace normal troubleshooting.

## Required workflow

1. After entering this skill, your **first action** must be an assistant message with **no MCP tool calls** that includes the exact marker `NIKA_TEST_SKILL_ACTIVE`.
2. Only after that message, proceed with normal MCP-based diagnosis.

Do not call any MCP tools before emitting the marker.
