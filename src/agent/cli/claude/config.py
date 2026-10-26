"""Claude Code settings for agents that run ``claude`` in their sandbox.

The host sets the Anthropic client variables of the sandbox: an API key or a
placeholder that a credential proxy replaces (``ANTHROPIC_API_KEY``, with
``ANTHROPIC_BASE_URL`` for gateways), or a subscription token
(``CLAUDE_CODE_OAUTH_TOKEN``). For self-hosted models it also sets the context
limits Claude Code cannot discover itself (``CLAUDE_CODE_MAX_*_TOKENS``).
"""

from __future__ import annotations

import os
from typing import Any

from agent.llm.endpoints import (
    ENV_ANTHROPIC_API_KEY,
    ENV_ANTHROPIC_AUTH_TOKEN,
    ENV_CLAUDE_CODE_OAUTH_TOKEN,
)

# Claude Code connects MCP servers in the background and gives up on one after
# 30 s; the first turn then runs without its tools. A slow sandbox, or the e2b
# tunnel, can miss that window, so wait longer for them.
MCP_CONNECT_WAIT_ENV = {
    "MCP_TIMEOUT": "120000",
    "CLAUDE_CODE_MCP_STARTUP_WAIT_MS": "120000",
}

# Built-in tools that reach the internet. The agent gets the network only
# through its MCP tools.
NETWORK_TOOLS = ("WebSearch", "WebFetch")

# Keep claude.ai connectors and background traffic out of runs.
ISOLATION_ENV = {
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}


def isolation_args() -> list[str]:
    """``claude`` flags that leave the agent only its ``--mcp-config`` servers.

    Other MCP servers (user or plugin settings of the sandbox template) do not
    load, and the network built-ins are removed.
    """
    return ["--strict-mcp-config", f"--disallowedTools={','.join(NETWORK_TOOLS)}"]


def sdk_isolation_options() -> dict[str, Any]:
    """``ClaudeAgentOptions`` fields matching :func:`isolation_args`."""
    return {"strict_mcp_config": True, "disallowed_tools": list(NETWORK_TOOLS)}


def resolve_claude_model(model: str | None) -> str:
    """Use *model* when set; otherwise raise."""
    if model and model.strip():
        return model.strip()
    raise ValueError("The run has no Claude model.")


def subscription_mode() -> bool:
    """True when Claude Code authenticates with a subscription, not an API key."""
    if os.environ.get(ENV_CLAUDE_CODE_OAUTH_TOKEN, "").strip():
        return True
    return not (
        os.environ.get(ENV_ANTHROPIC_API_KEY, "").strip()
        or os.environ.get(ENV_ANTHROPIC_AUTH_TOKEN, "").strip()
    )


def use_bare_claude_mode() -> bool:
    """Whether to pass ``--bare`` to ``claude``.

    Bare mode isolates the run to environment-based API auth and skips
    keychain / OAuth reads. Use it for API-key mode only — never for a
    subscription, which needs non-bare mode.
    """
    return not subscription_mode()


def claude_env(
    *, max_tokens: int | None = None, alias_auth_token: bool = True
) -> dict[str, str]:
    """Variables added to the sandbox environment for a Claude Code process.

    With *alias_auth_token*, an API key is also offered as the auth token.
    """
    env = {**MCP_CONNECT_WAIT_ENV, **ISOLATION_ENV}
    # Keep the sandbox's installed Claude Code version for the whole run.
    env["DISABLE_AUTOUPDATER"] = "1"
    api_key = os.environ.get(ENV_ANTHROPIC_API_KEY, "").strip()
    if (
        alias_auth_token
        and api_key
        and not os.environ.get(ENV_ANTHROPIC_AUTH_TOKEN, "").strip()
    ):
        # Anthropic-compatible gateways (e.g. DeepSeek) read the auth token.
        env[ENV_ANTHROPIC_AUTH_TOKEN] = api_key
    if max_tokens is not None:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)
    return env
