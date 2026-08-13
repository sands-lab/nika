"""SADE community agent — Symptom-Aware Diagnostic Escalation over Claude Code.

Implements the ``agent.protocols.TroubleshootingAgent`` contract
(``session_id`` + ``async def run(task_description) -> dict``) and is selected
via ``nika agent run -a community.sade``.

Unlike the LangGraph paths, SADE drives a single Claude Code session
(``claude-agent-sdk``) with a phase-gated system prompt and a 15-skill library
loaded from this package's ``.claude/`` directory. It still produces the same
diagnosis -> submission outcome through NIKA's Kathara + task MCP servers, and
writes structured events to the session's ``messages.jsonl`` in the schema the
NIKA trace parser/evaluator expects.

Reference: SADE (arXiv:2605.04530), built on NIKA.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from dotenv import load_dotenv

from agent.sdk.mcp import to_sdk_mcp_servers
from agent.sandbox.sdk_context import resolve_sdk_session_fields
from agent.utils.loggers import MessageLogger
from agent.utils.mcp_client import load_session_mcp_config
from agent.protocols import DIAGNOSIS
from agent.utils.skills import CLAUDE_SETTING_SOURCES, skills_enabled
from agent.utils.usage import normalize_usage

from .config import prepare_sade_sdk_env
from .prompts.sade_prompt import SADE_PROMPT

load_dotenv()

logger = logging.getLogger(__name__)

# Directory holding this agent's `.claude/` skill library, `CLAUDE.md`, and the
# `h.py` helper launcher. The Claude Code SDK uses it as the working directory
# so skills and helpers resolve with simple relative paths (`python h.py ...`).
PACKAGE_DIR = Path(__file__).resolve().parent

# SADE runs diagnosis + submission in one Claude Code session; tag events with
# the diagnosis phase id so the NIKA trace parser counts tool_calls/steps.
AGENT_TAG = DIAGNOSIS

# Fraction of the turn budget at which a single workflow reminder is injected.
TURN_REMINDER_FRAC = 0.50

SADE_REMINDER = (
    "SADE REMINDER: API turn {turn}/{total} ({remaining} remaining). "
    "If direct evidence on the owning device already matches a fault-family "
    "fingerprint, submit NOW — do not hypothesize secondary mechanisms the "
    "topology does not support. If you have a symptom but no owner yet, stay "
    "on that lead and stop broad probing. If you still have no symptom, do one "
    "broad lower-to-higher-layer escalation sweep, then submit `is_anomaly=False` "
    "only if that sweep finds nothing. Check the submit() signature in CLAUDE.md "
    "before calling — wrong types end the session."
)


class SadeAgent:
    """SADE: phase-gated Claude Code agent with the 15-skill library.

    Implements ``agent.protocols.TroubleshootingAgent``. Diagnosis and
    submission run inside a single Claude Code session: diagnosis tools come
    from the Kathara MCP servers, submission via the task MCP server's
    ``submit`` tool. Structured events are written to ``messages.jsonl``.
    """

    def __init__(
        self,
        session_id: str,
        model: str = "claude-sonnet-4-6",
        max_steps: int = 20,
        **_: Any,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.max_steps = max_steps
        self.session_dir, scenario_name = resolve_sdk_session_fields(session_id)

        self.mcp_servers = to_sdk_mcp_servers(
            load_session_mcp_config(
                session_id,
                scenario_name,
            )
        )

    async def run(self, task_description: str) -> dict[str, Any]:
        sdk_env = prepare_sade_sdk_env(session_id=self.session_id)
        msg_logger = MessageLogger(agent=AGENT_TAG, session_dir=self.session_dir)
        logger.info("sade: starting session %s", self.session_id)

        options_kwargs: dict[str, Any] = {
            "system_prompt": SADE_PROMPT,
            "model": self.model,
            "cwd": str(PACKAGE_DIR),
            "mcp_servers": self.mcp_servers,
            "max_turns": self.max_steps,
            "permission_mode": "bypassPermissions",
            "env": sdk_env,
        }
        if skills_enabled():
            options_kwargs["setting_sources"] = CLAUDE_SETTING_SOURCES

        options = ClaudeAgentOptions(**options_kwargs)

        msg_logger.log(
            "llm_start",
            {
                "messages": {"role": "user", "content": task_description},
                "model": {"name": self.model},
                "mcp_servers": list(self.mcp_servers.keys()),
            },
        )

        result_text = ""
        api_turn_count = 0
        reminded = False
        has_submitted = False
        reminder_at = int(self.max_steps * TURN_REMINDER_FRAC)
        in_tokens = 0
        out_tokens = 0
        turn_text: list[str] = []

        def _flush_turn() -> None:
            """Emit the accumulated assistant turn as one canonical ``llm_end``.

            NIKA's parser counts ``llm_end`` events as steps and the LLM judge
            reads ``text`` as the agent's response, so each turn's reasoning is
            emitted here. Token usage is reported once at the ResultMessage:
            per-message SDK usage is a streamed partial that repeats across a
            turn and would mis-sum.
            """
            nonlocal turn_text
            if turn_text:
                msg_logger.log(
                    "llm_end", {"text": "\n".join(turn_text), "usage_metadata": {}}
                )
                turn_text = []

        async with ClaudeSDKClient(options=options) as client:
            await client.query(task_description)
            async for message in client.receive_messages():
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    logger.info(
                        "sade: session started - %s",
                        message.data.get("session_id"),
                    )
                elif isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ThinkingBlock):
                            api_turn_count += 1
                            turn_text.append(block.thinking)
                        elif isinstance(block, TextBlock):
                            turn_text.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            # Emit the reasoning that led to this call, then the
                            # tool call (llm_end -> tool_start order, like NIKA).
                            _flush_turn()
                            msg_logger.log(
                                "tool_start",
                                {
                                    "tool": {"name": block.name},
                                    "input": str(block.input),
                                },
                            )
                            if "submit" in block.name:
                                has_submitted = True
                elif isinstance(message, UserMessage):
                    _flush_turn()  # close the turn that called these tools
                    content = (
                        message.content if isinstance(message.content, list) else []
                    )
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            if block.is_error:
                                msg_logger.log(
                                    "tool_error", {"output": str(block.content)}
                                )
                            else:
                                msg_logger.log(
                                    "tool_end",
                                    {
                                        "output": str(block.content),
                                        "output_type": "tool_result",
                                    },
                                )
                    if (
                        not reminded
                        and not has_submitted
                        and api_turn_count >= reminder_at
                    ):
                        reminded = True
                        remaining = self.max_steps - api_turn_count
                        text = SADE_REMINDER.format(
                            turn=api_turn_count,
                            total=self.max_steps,
                            remaining=remaining,
                        )
                        await client.query(text)
                        logger.info(
                            "sade: REMINDER at API turn %s/%s",
                            api_turn_count,
                            self.max_steps,
                        )
                elif isinstance(message, ResultMessage):
                    _flush_turn()  # flush any trailing assistant text
                    result_text = message.result or ""
                    md = normalize_usage(message.usage)
                    in_tokens = md["input_tokens"]
                    out_tokens = md["output_tokens"]
                    # Final `llm_end`: the agent's result text + the authoritative
                    # cumulative token usage (the parser sums usage_metadata).
                    msg_logger.log(
                        "llm_end", {"text": result_text, "usage_metadata": md}
                    )
                    logger.info(
                        "sade: session complete - stop_reason=%s, submitted=%s, "
                        "api_turns=%s, sdk_turns=%s, in_tokens=%s, out_tokens=%s",
                        message.stop_reason,
                        has_submitted,
                        api_turn_count,
                        message.num_turns,
                        in_tokens,
                        out_tokens,
                    )
                    break

        return {
            "result": result_text,
            "has_submitted": has_submitted,
            "api_turns": api_turn_count,
            "in_tokens": in_tokens,
            "out_tokens": out_tokens,
        }
