"""AutoGen diagnosis phase worker."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from autogen_agentchat.agents import AssistantAgent
from autogen_ext.tools.mcp import create_mcp_server_session, mcp_server_tools

from agent.byo.autogen.config import session_server_configs, to_mcp_params
from agent.byo.autogen.runner import create_model_client, run_logged_agent
from agent.utils.loggers import MessageLogger
from agent.protocols import DIAGNOSIS
from agent.utils.template import OVERALL_DIAGNOSIS_PROMPT


@asynccontextmanager
async def _open_mcp_tools(server_configs: dict) -> AsyncIterator[list]:
    sessions: list = []
    tools: list = []
    try:
        for cfg in server_configs.values():
            params = to_mcp_params(cfg)
            session_cm = create_mcp_server_session(params)
            session = await session_cm.__aenter__()
            await session.initialize()
            sessions.append(session_cm)
            tools.extend(await mcp_server_tools(params, session=session))
        yield tools
    finally:
        for session_cm in reversed(sessions):
            await session_cm.__aexit__(None, None, None)


class AutogenDiagnosisPhase:
    """Run network fault diagnosis via AutoGen AssistantAgent + MCP tools."""

    def __init__(
        self,
        session_id: str,
        session_dir: str,
        model: str,
        max_steps: int,
        scenario_name: str,
        *,
        reasoning_effort: str | None = None,
    ) -> None:
        self._session_id = session_id
        self._session_dir = session_dir
        self._model = model
        self._max_steps = max_steps
        self._reasoning_effort = reasoning_effort
        self._server_configs = session_server_configs(session_id, scenario_name)

    async def run(self, task_description: str) -> tuple[str, bool]:
        """Return ``(diagnosis_report, is_max_steps_reached)``."""
        logger = MessageLogger(agent=DIAGNOSIS, session_dir=self._session_dir)
        model_client = create_model_client(
            self._model, reasoning_effort=self._reasoning_effort
        )

        async with _open_mcp_tools(self._server_configs) as tools:
            agent = AssistantAgent(
                name=DIAGNOSIS,
                model_client=model_client,
                tools=tools,
                system_message=OVERALL_DIAGNOSIS_PROMPT,
                reflect_on_tool_use=True,
                max_tool_iterations=self._max_steps,
            )
            try:
                report, is_max_steps_reached = await run_logged_agent(
                    agent=agent,
                    task=f"Task: {task_description}",
                    logger=logger,
                    max_steps=self._max_steps,
                )
            finally:
                await model_client.close()

        return report, is_max_steps_reached
