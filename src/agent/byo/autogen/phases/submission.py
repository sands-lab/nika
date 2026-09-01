"""AutoGen submission phase worker."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from autogen_agentchat.agents import AssistantAgent
from autogen_ext.tools.mcp import create_mcp_server_session, mcp_server_tools

from agent.byo.autogen.config import session_server_configs, to_mcp_params
from agent.byo.autogen.runner import create_model_client, run_logged_agent
from agent.utils.loggers import MessageLogger
from agent.utils.mcp_client import begin_submission_mcp_phase
from agent.protocols import SUBMISSION
from agent.utils.template import SUBMIT_PROMPT_TEMPLATE
from agent.utils.submission_context import submission_prompt_context


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


class AutogenSubmissionPhase:
    """Submit structured results via the task MCP server."""

    def __init__(
        self,
        session_id: str,
        session_dir: str,
        model: str,
        max_steps: int,
        scenario_name: str,
        *,
        llm_provider: str,
        reasoning_effort: str | None = None,
    ) -> None:
        self._session_id = session_id
        self._session_dir = session_dir
        self._model = model
        self._max_steps = max_steps
        self._scenario_name = scenario_name
        self._llm_provider = llm_provider
        self._reasoning_effort = reasoning_effort

    async def run(self, diagnosis_report: str) -> str:
        begin_submission_mcp_phase(self._session_id, diagnosis_report)
        server_configs = session_server_configs(
            self._session_id,
            self._scenario_name,
        )
        logger = MessageLogger(phase=SUBMISSION, session_dir=self._session_dir)
        model_client = create_model_client(
            self._model,
            provider=self._llm_provider,
            reasoning_effort=self._reasoning_effort,
        )
        prompt = (
            f"Based on the diagnosis report: {diagnosis_report}. "
            f"{submission_prompt_context(self._session_id)} "
            "please provide the submission. Do not submit if no report available."
        )

        async with _open_mcp_tools(server_configs) as tools:
            agent = AssistantAgent(
                name=SUBMISSION,
                model_client=model_client,
                tools=tools,
                system_message=SUBMIT_PROMPT_TEMPLATE,
                reflect_on_tool_use=True,
                max_tool_iterations=self._max_steps,
            )
            try:
                result, _ = await run_logged_agent(
                    agent=agent,
                    task=prompt,
                    logger=logger,
                    max_steps=self._max_steps,
                )
            finally:
                await model_client.close()

        return result
