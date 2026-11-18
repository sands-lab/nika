"""Claude / Codex SDK agents.

Direct integration with vendor SDKs, bypassing LangChain chat models:
- ``sdk.claude_sdk`` — ``claude-agent-sdk``
- ``sdk.codex_sdk`` — ``openai-codex``

Their sandboxes install the SDK packages. CLI-based agents live under
``agent.cli``. Both phases (diagnosis → submission) write to
``{session_dir}/messages.jsonl`` via ``MessageLogger`` with the same event
schema as other agents.
"""

__all__: list[str] = []
