"""AutoGen AgentChat agent.

Two-phase troubleshooting pipeline via :class:`~autogen_agentchat.teams.GraphFlow`.

Select with ``nika agent run -a byo.autogen``.
"""

from __future__ import annotations

import logging
from typing import Any

from agent.byo.autogen.workflow import run_troubleshooting_flow
from nika.utils.session import Session

logging.basicConfig(level=logging.INFO)


class AutogenAgent:
    """Two-phase troubleshooting agent using AutoGen ``GraphFlow``."""

    def __init__(
        self,
        session_id: str,
        model: str = "gpt-4.1-mini",
        max_steps: int = 20,
        *,
        reasoning_effort: str | None = None,
        stream_output: bool = True,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.max_steps = max_steps
        self.reasoning_effort = reasoning_effort
        self._stream_output = stream_output

        session = Session()
        session.load_running_session(session_id=session_id)
        self.session = session
        self.session_dir: str = session.session_dir

        self._scenario_name: str = getattr(session, "scenario_name", "")

    async def run(self, task_description: str) -> dict[str, Any]:
        return await run_troubleshooting_flow(
            task_description=task_description,
            session_id=self.session_id,
            session_dir=self.session_dir,
            model=self.model,
            max_steps=self.max_steps,
            scenario_name=self._scenario_name,
            reasoning_effort=self.reasoning_effort,
            stream_output=self._stream_output,
        )
