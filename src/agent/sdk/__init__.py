"""Claude / Codex SDK agents.

Direct integration with vendor SDKs, bypassing LangChain chat models:
- ``sdk.claude_sdk`` — ``claude-agent-sdk`` (DeepSeek via Anthropic-compatible API)
- ``sdk.codex_sdk`` — ``openai-codex`` (local ``~/.codex/auth.json``)

Install optional dependencies::

    uv sync --extra sdk --prerelease=allow

CLI-based agents live under ``agent.cli``:
- ``agent.cli.claude`` — Claude Code CLI subprocess workers
- ``agent.cli.codex`` — Codex CLI subprocess workers

Both phases (diagnosis → submission) write to ``{session_dir}/messages.jsonl``
via ``MessageLogger`` with the same event schema as other agents.
"""

__all__: list[str] = []
