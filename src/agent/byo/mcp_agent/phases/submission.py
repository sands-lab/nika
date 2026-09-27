"""mcp-agent submission phase worker."""

from __future__ import annotations

import agent.byo.mcp_agent._bootstrap  # noqa: F401

from mcp_agent.agents.agent import Agent

from agent.byo.mcp_agent.config import _mcp_reasoning_effort, build_mcp_request_params
from agent.byo.mcp_agent.llm import create_nika_augmented_llm
from agent.utils.loggers import MessageLogger
from agent.protocols import SUBMISSION
from agent.utils.template import SUBMIT_PROMPT_TEMPLATE
from agent.utils.submission_context import submission_user_prompt


class McpSubmissionPhase:
    """Submit structured results via the task MCP server."""

    def __init__(
        self,
        session_dir: str,
        model: str,
        max_steps: int,
        server_names: list[str],
        *,
        llm_provider: str,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self._session_dir = session_dir
        self._model = model
        self._max_steps = max_steps
        self._server_names = server_names
        self._llm_provider = llm_provider
        self._reasoning_effort = _mcp_reasoning_effort(reasoning_effort)
        self._max_tokens = max_tokens

    async def run(self, diagnosis_report: str, context: dict) -> str:
        logger = MessageLogger(phase=SUBMISSION, session_dir=self._session_dir)
        request_params = build_mcp_request_params(
            model=self._model,
            max_steps=self._max_steps,
            reasoning_effort=self._reasoning_effort,
            max_tokens=self._max_tokens,
            provider=self._llm_provider,
        )
        prompt = submission_user_prompt(diagnosis_report, context)

        agent = Agent(
            name=SUBMISSION,
            instruction=SUBMIT_PROMPT_TEMPLATE,
            server_names=self._server_names,
        )
        async with agent:
            llm = create_nika_augmented_llm(
                agent=agent,
                nika_logger=logger,
                default_request_params=request_params,
                provider=self._llm_provider,
            )
            await agent.attach_llm(llm=llm)
            return await llm.generate_str(prompt, request_params=request_params)
