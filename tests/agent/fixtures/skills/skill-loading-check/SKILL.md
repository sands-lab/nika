---
name: skill-loading-check
description: Integration-test skill only. Invoke at the start of every troubleshooting session before other diagnostics to confirm the skill library is loaded.
---

# Skill Loading Check

This skill exists only to verify skill loading in automated tests. It does not replace normal troubleshooting.

## Required workflow

1. After entering this skill, your **first action** must be an assistant message with **no MCP tool calls** that includes the exact marker `SKILL_LOADING_CHECK_ACTIVE`.
2. Only after that message, proceed with normal MCP-based diagnosis.

Do not call any MCP tools before emitting the marker.
