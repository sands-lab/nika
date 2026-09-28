"""AutoGen AgentChat agent.

Two-phase troubleshooting pipeline on :class:`~agent.utils.two_phase.TwoPhaseAgent`;
each phase is an AutoGen ``AssistantAgent`` with the phase's MCP tools.

Select with ``nika agent run -a byo.autogen``.
"""

from __future__ import annotations

import logging

from agent.byo.autogen.config import session_server_configs
from agent.byo.autogen.runner import run_autogen_phase
from agent.protocols import DIAGNOSIS, SUBMISSION
from agent.utils.loggers import MessageLogger
from agent.utils.submission_context import submission_user_prompt
from agent.utils.template import OVERALL_DIAGNOSIS_PROMPT, SUBMIT_PROMPT_TEMPLATE
from agent.utils.two_phase import TwoPhaseAgent
from nika.utils.session import Session

logging.basicConfig(level=logging.INFO)


class AutogenAgent(TwoPhaseAgent):
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
        self.session_id = session_id
        self.model = model
        self.max_steps = max_steps
        self.llm_provider = llm_provider
        self.reasoning_effort = reasoning_effort
        self.max_tokens = max_tokens
        self.stream_output = stream_output

        session = Session()
        session.load_running_session(session_id=session_id)
        self.session = session
        self.session_dir: str = session.session_dir
        self.trace_dir = self.session_dir
        self._scenario_name: str = getattr(session, "scenario_name", "")

    async def _run_phase(self, phase: str, system_message: str, task: str) -> str:
        return await run_autogen_phase(
            name=phase,
            system_message=system_message,
            task=task,
            server_configs=session_server_configs(
                self.session_id, self._scenario_name, phase
            ),
            model=self.model,
            provider=self.llm_provider,
            reasoning_effort=self.reasoning_effort,
            max_tokens=self.max_tokens,
            max_steps=self.max_steps,
            logger=MessageLogger(phase=phase, session_dir=self.session_dir),
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
