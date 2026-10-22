"""AutoGen AgentChat agent.

Two-phase troubleshooting pipeline on :class:`~agent.base.DiagnosisAgent`;
each phase is an AutoGen ``AssistantAgent`` with the phase's MCP tools.
"""

from __future__ import annotations

import logging

from agent.byo.autogen.runner import run_autogen_phase
from agent.protocols import DIAGNOSIS, SUBMISSION
from agent.utils.loggers import MessageLogger
from agent.utils.mcp_client import session_mcp_config
from agent.utils.submission_context import submission_user_prompt
from agent.utils.template import OVERALL_DIAGNOSIS_PROMPT, SUBMIT_PROMPT_TEMPLATE
from agent.base import DiagnosisAgent

logging.basicConfig(level=logging.INFO)


class AutogenAgent(DiagnosisAgent):
    """Two-phase troubleshooting agent using AutoGen ``AssistantAgent``."""

    def __init__(
        self,
        session_id: str,
        model: str = "gpt-4.1-mini",
        max_steps: int = 20,
        *,
        llm_provider: str,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
        stream_output: bool = True,
    ) -> None:
        super().__init__(
            session_id,
            model=model,
            llm_provider=llm_provider,
            max_steps=max_steps,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            stream_output=stream_output,
        )

    async def _run_phase(self, phase: str, system_message: str, task: str) -> str:
        return await run_autogen_phase(
            name=phase,
            system_message=system_message,
            task=task,
            server_configs=session_mcp_config(phase),
            model=self.model,
            provider=self.llm_provider,
            reasoning_effort=self.reasoning_effort,
            max_tokens=self.max_tokens,
            max_steps=self.max_steps,
            logger=MessageLogger(phase=phase, session_dir=self.trace_dir),
        )

    async def diagnose(self, task_description: str) -> str:
        return await self._run_phase(
            DIAGNOSIS, OVERALL_DIAGNOSIS_PROMPT, f"Task: {task_description}"
        )

    async def submit(self, diagnosis_report: str, context: dict) -> str:
        return await self._run_phase(
            SUBMISSION,
            SUBMIT_PROMPT_TEMPLATE,
            submission_user_prompt(diagnosis_report, context),
        )
