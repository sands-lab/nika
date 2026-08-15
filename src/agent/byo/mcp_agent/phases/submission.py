"""mcp-agent submission phase worker."""

from __future__ import annotations

from mcp_agent.agents.agent import Agent

from agent.byo.mcp_agent.config import _mcp_reasoning_effort, build_mcp_request_params
from agent.byo.mcp_agent.llm import create_nika_augmented_llm
from agent.utils.loggers import MessageLogger
from agent.utils.mcp_client import begin_submission_mcp_phase
from agent.protocols import SUBMISSION
from agent.utils.template import SUBMIT_PROMPT_TEMPLATE


class McpSubmissionPhase:
    """Submit structured results via the task MCP server."""

    def __init__(
        self,
        session_id: str,
        session_dir: str,
        model: str,
        max_steps: int,
        server_names: list[str],
        *,
        llm_provider: str,
        reasoning_effort: str | None = None,
    ) -> None:
        self._session_id = session_id
        self._session_dir = session_dir
        self._model = model
        self._max_steps = max_steps
        self._server_names = server_names
        self._llm_provider = llm_provider
        self._reasoning_effort = _mcp_reasoning_effort(reasoning_effort)

    async def run(self, diagnosis_report: str) -> str:
        begin_submission_mcp_phase(self._session_id)
        logger = MessageLogger(agent=SUBMISSION, session_dir=self._session_dir)
        request_params = build_mcp_request_params(
            model=self._model,
            max_steps=self._max_steps,
            reasoning_effort=self._reasoning_effort,
            provider=self._llm_provider,
        )
        prompt = (
            f"{SUBMIT_PROMPT_TEMPLATE}\n\n"
            f"Based on the diagnosis report: {diagnosis_report}\n"
            "Please provide the submission. Do not submit if no report is available."
        )

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
