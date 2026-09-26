"""mcp-agent SDK agent.

Two-phase troubleshooting pipeline on :class:`~agent.utils.two_phase.TwoPhaseAgent`,
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
from agent.utils.two_phase import TwoPhaseAgent
from nika.utils.session import Session

logging.basicConfig(level=logging.INFO)


class McpAgent(TwoPhaseAgent):
    """Two-phase troubleshooting agent using mcp-agent ``Agent`` + AugmentedLLM."""

    def __init__(
        self,
        session_id: str,
        model: str = "gpt-4.1-mini",
        max_steps: int = 20,
        *,
        llm_provider: str,
        reasoning_effort: str | None = None,
        stream_output: bool = True,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.max_steps = max_steps
        self.llm_provider = llm_provider
        self.reasoning_effort = reasoning_effort
        self.stream_output = stream_output

        session = Session()
        session.load_running_session(session_id=session_id)
        self.session = session
        self.session_dir: str = session.session_dir
        self.trace_dir = self.session_dir

        self._scenario_name: str = getattr(session, "scenario_name", "")
        names = session_server_names(self._scenario_name)
        self._server_names = {
            phase: list(filter_phase_servers(dict.fromkeys(names), phase))
            for phase in (DIAGNOSIS, SUBMISSION)
        }

    async def run(self, task_description: str) -> dict[str, Any]:
        """Execute the two-phase pipeline inside an MCPApp context."""
        settings = build_mcp_agent_settings(
            session_id=self.session_id,
            scenario_name=self._scenario_name,
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
        ).run(task_description)

    async def submit(self, diagnosis_report: str, context: dict) -> str:
        return await McpSubmissionPhase(
            session_dir=self.session_dir,
            model=self.model,
            max_steps=self.max_steps,
            server_names=self._server_names[SUBMISSION],
            llm_provider=self.llm_provider,
            reasoning_effort=self.reasoning_effort,
        ).run(diagnosis_report, context)
