"""mcp-agent SDK agent.

Two-phase troubleshooting pipeline via :class:`~agent.byo.mcp_agent.workflow.NikaTroubleshootingWorkflow`.

Select with ``nika agent run -a byo.mcp_agent``.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp_agent.app import MCPApp

from agent.byo.mcp_agent.config import build_mcp_agent_settings
from agent.byo.mcp_agent.workflow import NikaTroubleshootingWorkflow
from nika.utils.session import Session

logging.basicConfig(level=logging.INFO)


class McpAgent:
    """Two-phase troubleshooting agent using mcp-agent ``Workflow``."""

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
        self._stream_output = stream_output

        session = Session()
        session.load_running_session(session_id=session_id)
        self.session = session
        self.session_dir: str = session.session_dir

        self._scenario_name: str = getattr(session, "scenario_name", "")

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
        async with app.run() as running_app:
            workflow = NikaTroubleshootingWorkflow(
                context=running_app.context,
                session_id=self.session_id,
                session_dir=self.session_dir,
                model=self.model,
                max_steps=self.max_steps,
                scenario_name=self._scenario_name,
                llm_provider=self.llm_provider,
                reasoning_effort=self.reasoning_effort,
                stream_output=self._stream_output,
            )
            await workflow.initialize()
            try:
                result = await workflow.run(task_description)
            finally:
                await workflow.cleanup()
            return result.value or {}
