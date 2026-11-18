"""Files and settings shared by the host and an agent in its sandbox.

The host writes ``task.json`` into the agent workspace and starts
``python -m agent.runtime <phase>`` there. The agent reads everything it needs
from the workspace and the standard model client variables (``OPENAI_*`` or
``ANTHROPIC_*``); it never imports host code. The diagnosis step leaves
``report.json``; the host freezes it, writes ``submission_context.json``, and
starts the submission step.
"""

from __future__ import annotations

# Absolute path of the agent workspace inside the sandbox.
ENV_WORKSPACE = "AGENT_WORKSPACE"

TASK_FILENAME = "task.json"
REPORT_FILENAME = "report.json"
SUBMISSION_CONTEXT_FILENAME = "submission_context.json"
# Event trace of both phases, one JSON object per line:
# ``{timestamp, phase, event, **payload}``.
MESSAGES_FILENAME = "messages.jsonl"
# Variables the runner exports before it imports the agent (credential
# placeholders and client settings that must not live in task.json).
RUNTIME_ENV_FILENAME = ".runtime_env.json"

# A phase result that starts with this prefix reports a failure.
ERROR_PREFIX = "ERROR:"

# MCP gateway headers: the opaque session handle and its access token.
SESSION_HEADER = "X-Session-Id"
SESSION_TOKEN_HEADER = "X-Session-Token"

# Task MCP server, the only server of the submission phase.
SUBMISSION_SERVER = "task_mcp_server"
