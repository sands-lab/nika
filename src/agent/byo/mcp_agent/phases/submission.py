"""mcp-agent submission phase worker."""

from __future__ import annotations

from mcp_agent.agents.agent import Agent
from mcp_agent.workflows.llm.augmented_llm import RequestParams

from agent.byo.mcp_agent.llm import NikaOpenAIAugmentedLLM
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
    ) -> None:
        self._session_id = session_id
        self._session_dir = session_dir
        self._model = model
        self._max_steps = max_steps
        self._server_names = server_names

    async def run(self, diagnosis_report: str) -> str:
        begin_submission_mcp_phase(self._session_id)
        logger = MessageLogger(agent=SUBMISSION, session_dir=self._session_dir)
        request_params = RequestParams(
            model=self._model,
            max_iterations=self._max_steps,
            temperature=0,
            use_history=False,
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
            llm = NikaOpenAIAugmentedLLM(
                agent=agent,
                nika_logger=logger,
                default_request_params=request_params,
            )
            await agent.attach_llm(llm=llm)
            return await llm.generate_str(prompt, request_params=request_params)
