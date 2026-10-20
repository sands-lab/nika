"""mcp-agent SDK agent.

Two-phase troubleshooting pipeline on :class:`~agent.base.NikaAgent`,
with both phases inside one ``MCPApp`` context.

Select with ``nika agent run -a byo.mcp_agent``.
"""

from __future__ import annotations

import logging
from typing import Any

import agent.byo.mcp_agent._bootstrap  # noqa: F401

from mcp_agent.app import MCPApp

from agent.byo.mcp_agent.config import build_mcp_agent_settings, session_server_names
from agent.byo.mcp_agent.phases.diagnosis import McpDiagnosisPhase
from agent.byo.mcp_agent.phases.submission import McpSubmissionPhase
from agent.protocols import DIAGNOSIS, SUBMISSION
from agent.utils.mcp_client import filter_phase_servers
from agent.base import NikaAgent

logging.basicConfig(level=logging.INFO)


class McpAgent(NikaAgent):
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
        names = session_server_names(self.scenario_name)
        self._server_names = {
            phase: list(filter_phase_servers(dict.fromkeys(names), phase))
            for phase in (DIAGNOSIS, SUBMISSION)
        }

    async def run(self, task_description: str) -> dict[str, Any]:
        """Execute the two-phase pipeline inside an MCPApp context."""
        settings = build_mcp_agent_settings(
            session_id=self.session_id,
            scenario_name=self.scenario_name,
            model=self.model,
            provider=self.llm_provider,
            reasoning_effort=self.reasoning_effort,
        )
        app = MCPApp(
            name="nika_mcp_agent", settings=settings, session_id=self.session_id
        )
        async with app.run():
            return await super().run(task_description)

    async def diagnose(self, task_description: str) -> str:
        return await McpDiagnosisPhase(
            session_dir=self.session_dir,
            model=self.model,
            max_steps=self.max_steps,
            server_names=self._server_names[DIAGNOSIS],
            llm_provider=self.llm_provider,
            reasoning_effort=self.reasoning_effort,
            max_tokens=self.max_tokens,
        ).run(task_description)

    async def submit(self, diagnosis_report: str, context: dict) -> str:
        return await McpSubmissionPhase(
            session_dir=self.session_dir,
            model=self.model,
            max_steps=self.max_steps,
            server_names=self._server_names[SUBMISSION],
            llm_provider=self.llm_provider,
            reasoning_effort=self.reasoning_effort,
            max_tokens=self.max_tokens,
        ).run(diagnosis_report, context)
