"""SADE community agent — Symptom-Aware Diagnostic Escalation over Claude Code.

Selected via ``nika agent run -a community.sade``. SADE drives Claude Code
sessions (``claude-agent-sdk``) with a phase-gated system prompt and a
15-skill library loaded from this package's ``.claude/`` directory. It follows
the shared two-phase contract (:class:`~agent.utils.two_phase.TwoPhaseAgent`):
a diagnosis session with the lab MCP servers, then a submission session with
only the task MCP server.

Reference: SADE (arXiv:2605.04530), built on NIKA.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from agent.protocols import DIAGNOSIS, SUBMISSION
from agent.sandbox.sdk_context import resolve_sdk_session_fields
from agent.sdk.claude_sdk.transcript import ClaudeSdkTranscript, run_claude_sdk_query
from agent.sdk.mcp import to_sdk_mcp_servers
from agent.utils.loggers import MessageLogger
from agent.utils.mcp_client import load_session_mcp_config
from agent.utils.skills import CLAUDE_SETTING_SOURCES, skills_enabled
from agent.utils.submission_context import submission_user_prompt
from agent.utils.template import SUBMIT_PROMPT_TEMPLATE
from agent.utils.two_phase import TwoPhaseAgent

from .config import prepare_sade_sdk_env
from .prompts.sade_prompt import SADE_PROMPT

load_dotenv()

logger = logging.getLogger(__name__)

# Directory holding this agent's `.claude/` skill library, `CLAUDE.md`, and the
# `h.py` helper launcher. The Claude Code SDK uses it as the working directory
# so skills and helpers resolve with simple relative paths (`python h.py ...`).
PACKAGE_DIR = Path(__file__).resolve().parent

# Fraction of the turn budget at which a single workflow reminder is injected.
TURN_REMINDER_FRAC = 0.50

SADE_REMINDER = (
    "SADE REMINDER: API turn {turn}/{total} ({remaining} remaining). "
    "If direct evidence on the owning device already matches a fault-family "
    "fingerprint, stop investigating and write your final diagnosis report "
    "now; do not hypothesize secondary mechanisms the topology does not "
    "support. If you have a symptom but no owner yet, stay on that lead and "
    "stop broad probing. If you still have no symptom, do one broad "
    "lower-to-higher-layer escalation sweep, then report no anomaly only if "
    "that sweep finds nothing."
)


class SadeAgent(TwoPhaseAgent):
    """SADE: phase-gated Claude Code agent with the 15-skill library."""

    def __init__(
        self,
        session_id: str,
        model: str = "claude-sonnet-4-6",
        max_steps: int = 20,
        *,
        llm_provider: str,
        stream_output: bool = True,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.max_steps = max_steps
        self.llm_provider = llm_provider
        self.stream_output = stream_output
        self.session_dir, self._scenario_name = resolve_sdk_session_fields(session_id)
        self.trace_dir = self.session_dir

    def _mcp_servers(self, phase: str) -> dict[str, Any]:
        return to_sdk_mcp_servers(
            load_session_mcp_config(self.session_id, self._scenario_name, phase=phase)
        )

    def _reminder_hook(self, transcript: ClaudeSdkTranscript):
        """PostToolUse hook: one budget reminder in the same query.

        A hook keeps the reminder inside the running turn loop, so it neither
        resets Claude Code's ``max_turns`` budget nor leaves a second query's
        reply unread.
        """
        reminder_at = max(1, int(self.max_steps * TURN_REMINDER_FRAC))
        state = {"sent": False}

        async def hook(_input: Any, _tool_use_id: str | None, _context: Any):
            if state["sent"] or transcript.turns < reminder_at:
                return {}
            state["sent"] = True
            logger.info(
                "sade: reminder at API turn %s/%s", transcript.turns, self.max_steps
            )
            text = SADE_REMINDER.format(
                turn=transcript.turns,
                total=self.max_steps,
                remaining=max(self.max_steps - transcript.turns, 0),
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": text,
                }
            }

        return hook

    async def diagnose(self, task_description: str) -> str:
        # claude-agent-sdk is installed in the sandbox only; importing it here
        # lets the host construct the agent without the SDK.
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

        sdk_env = prepare_sade_sdk_env(
            session_id=self.session_id, provider=self.llm_provider
        )
        msg_logger = MessageLogger(phase=DIAGNOSIS, session_dir=self.session_dir)
        transcript = ClaudeSdkTranscript(msg_logger, model=self.model)
        mcp_servers = self._mcp_servers(DIAGNOSIS)
        options_kwargs: dict[str, Any] = {
            "system_prompt": SADE_PROMPT + "\nDo not submit during diagnosis.",
            "model": self.model,
            "cwd": str(PACKAGE_DIR),
            "mcp_servers": mcp_servers,
            "max_turns": self.max_steps,
            "permission_mode": "bypassPermissions",
            "env": sdk_env,
            "hooks": {
                "PostToolUse": [HookMatcher(hooks=[self._reminder_hook(transcript)])]
            },
        }
        if skills_enabled():
            options_kwargs["setting_sources"] = CLAUDE_SETTING_SOURCES

        report = await run_claude_sdk_query(
            ClaudeAgentOptions(**options_kwargs),
            task_description,
            logger=msg_logger,
            transcript=transcript,
            max_steps=self.max_steps,
        )
        logger.info("sade: diagnosis done after %s API turns", transcript.turns)
        return report

    async def submit(self, diagnosis_report: str, context: dict) -> str:
        """Run SADE with only the final submit tool."""
        from claude_agent_sdk import ClaudeAgentOptions

        sdk_env = prepare_sade_sdk_env(
            session_id=self.session_id, provider=self.llm_provider
        )
        msg_logger = MessageLogger(phase=SUBMISSION, session_dir=self.session_dir)
        options = ClaudeAgentOptions(
            system_prompt=SUBMIT_PROMPT_TEMPLATE,
            model=self.model,
            cwd=str(PACKAGE_DIR),
            mcp_servers=self._mcp_servers(SUBMISSION),
            max_turns=self.max_steps,
            permission_mode="bypassPermissions",
            env=sdk_env,
        )
        return await run_claude_sdk_query(
            options,
            submission_user_prompt(diagnosis_report, context),
            logger=msg_logger,
            transcript=ClaudeSdkTranscript(msg_logger, model=self.model),
            max_steps=self.max_steps,
        )
