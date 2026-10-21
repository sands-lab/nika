"""mcp-agent SDK agent.

Two-phase troubleshooting pipeline on :class:`~agent.base.DiagnosisAgent`;
each phase runs inside an ``MCPApp`` context.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import agent.byo.mcp_agent._bootstrap  # noqa: F401

from mcp_agent.app import MCPApp

from agent.byo.mcp_agent.config import build_mcp_agent_settings
from agent.byo.mcp_agent.phases.diagnosis import McpDiagnosisPhase
from agent.byo.mcp_agent.phases.submission import McpSubmissionPhase
from agent.protocols import DIAGNOSIS, SUBMISSION
from agent.base import DiagnosisAgent
from agent.utils.mcp_client import session_mcp_config

logging.basicConfig(level=logging.INFO)


class McpAgent(DiagnosisAgent):
    """Two-phase troubleshooting agent using mcp-agent ``Agent`` + AugmentedLLM."""

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
        self._server_names = {
            phase: list(session_mcp_config(phase)) for phase in (DIAGNOSIS, SUBMISSION)
        }

    async def _in_app(self, run: Callable[[], Awaitable[str]]) -> str:
        """Run one phase inside an MCPApp context."""
        settings = build_mcp_agent_settings(
            model=self.model,
            provider=self.llm_provider,
            reasoning_effort=self.reasoning_effort,
        )
        app = MCPApp(name="mcp_agent", settings=settings, session_id=self.session_id)
        async with app.run():
            return await run()

    async def diagnose(self, task_description: str) -> str:
        return await self._in_app(lambda: self._diagnose(task_description))

    async def submit(self, diagnosis_report: str, context: dict) -> str:
        return await self._in_app(lambda: self._submit(diagnosis_report, context))

    async def _diagnose(self, task_description: str) -> str:
        return await McpDiagnosisPhase(
            session_dir=self.session_dir,
            model=self.model,
            max_steps=self.max_steps,
            server_names=self._server_names[DIAGNOSIS],
            llm_provider=self.llm_provider,
            reasoning_effort=self.reasoning_effort,
            max_tokens=self.max_tokens,
        ).run(task_description)

    async def _submit(self, diagnosis_report: str, context: dict) -> str:
        return await McpSubmissionPhase(
            session_dir=self.session_dir,
            model=self.model,
            max_steps=self.max_steps,
            server_names=self._server_names[SUBMISSION],
            llm_provider=self.llm_provider,
            reasoning_effort=self.reasoning_effort,
            max_tokens=self.max_tokens,
        ).run(diagnosis_report, context)
