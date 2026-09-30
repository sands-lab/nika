---
name: orchestrating-claude-code
description: Delegates substantial coding, debugging, refactoring, and review work to a stateless Claude Code CLI session running headlessly in a Docker Sandbox, then integrates and verifies its output. Use when Letta is in a remote development environment with Docker Sandboxes (sbx) available and a focused coding subtask would benefit from an isolated Claude session.
---

# Orchestrating Claude Code

Use Claude Code as a focused, stateless coding subagent inside Docker Sandbox; keep Letta responsible for understanding the task, supplying project/user context, reviewing the result, and reporting back. Prefer delegation for substantial or ambiguous code work, not trivial one-line changes.

## Workflow

1. **Own the task.** Read relevant project instructions, inspect Git status and target files, and form a bounded subtask. Do not hand Claude secrets, unrelated user data, or the entire conversation. Include the goal, acceptance criteria, relevant files, coding conventions/memories, constraints, and focused test commands because Claude has no Letta memory.
2. **Check the sandbox.** Verify `sbx` and Docker are available (`sbx version`, `docker info`) and inspect `sbx ls --json`. Reuse an existing sandbox only when it is clearly the right project/workspace and safe to reuse; never stop, remove, prune, reset, or log out of unrelated sandboxes. If unavailable, do the work directly in the current remote environment or report the blocker; do not run an unrestricted Claude session on the host as a substitute.
3. **Create an isolated workspace.** Prefer a uniquely named Claude sandbox for the current repo. Use `sbx create --name <unique-name> claude <repo-root>` to share only that project workspace. Use `--clone` when the task benefits from a private in-container Git clone; in clone mode, commit delegated changes on a task branch so they can be retrieved through the sandbox's Git remote. Never pass secrets through `--env`, `--env-file`, or the prompt.
4. **Run Claude headlessly.** Find the repository path inside the sandbox rather than assuming the host path is identical. Invoke Claude with `claude -p <focused-prompt> --model opus -C <sandbox-repo-path>`. Use `--dangerously-skip-permissions` only inside the isolated sandbox and only when its governance/network access is appropriate; otherwise retain Claude's permission checks. Capture the exit status and complete output. Keep the task to one coherent implementation/review pass; Letta can answer a Claude follow-up and resume the same Claude CLI session if necessary.
5. **Review and integrate.** Inspect Claude's report and the actual diff in the shared workspace (or fetch the private-clone task branch). Check for unrelated edits, regressions, and secret exposure; run relevant tests yourself when feasible. Do not blindly accept Claude's claims, merge its branch, push, or open a PR unless the user's request authorizes those actions.
6. **Clean up only what you own.** Preserve useful task evidence until integration and validation finish. Remove/stop only a sandbox created for this task, and only when its changes have been safely integrated and no work remains inside it. Leave pre-existing sandboxes untouched.

## Delegation prompt

Adapt this template to the actual task and project; keep it narrow:

```text
Implement/review: <specific outcome>
Repository/worktree: <path inside the sandbox>
Relevant context and conventions: <facts, memories, instructions, file paths>
Constraints: <what not to change; security/compatibility constraints>
Acceptance criteria: <observable requirements>
Validation: <focused tests/checks>
Report: summarize changed files, decisions, test results, and anything unresolved.
```

The Letta post at https://www.letta.com/blog/orchestrating-coding-agents/ describes this pattern: use Letta's persistent project/user context to brief a clean Claude context, and use Letta to review and interpret the stateless subagent's output.
