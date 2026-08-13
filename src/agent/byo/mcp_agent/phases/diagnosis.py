"""mcp-agent diagnosis phase worker."""

from __future__ import annotations

from mcp_agent.agents.agent import Agent
from mcp_agent.workflows.llm.augmented_llm import RequestParams

from agent.byo.mcp_agent.llm import NikaOpenAIAugmentedLLM
from agent.utils.loggers import MessageLogger
from agent.protocols import DIAGNOSIS
from agent.utils.template import OVERALL_DIAGNOSIS_PROMPT


class McpDiagnosisPhase:
    """Run network fault diagnosis via mcp-agent Agent + OpenAI."""

    def __init__(
        self,
        session_dir: str,
        model: str,
        max_steps: int,
        server_names: list[str],
    ) -> None:
        self._session_dir = session_dir
        self._model = model
        self._max_steps = max_steps
        self._server_names = server_names

    async def run(self, task_description: str) -> tuple[str, bool]:
        """Return ``(diagnosis_report, is_max_steps_reached)``."""
        logger = MessageLogger(agent=DIAGNOSIS, session_dir=self._session_dir)
        request_params = RequestParams(
            model=self._model,
            max_iterations=self._max_steps,
            temperature=0,
            use_history=False,
        )

        agent = Agent(
            name=DIAGNOSIS,
            instruction=OVERALL_DIAGNOSIS_PROMPT,
            server_names=self._server_names,
        )
        async with agent:
            llm = NikaOpenAIAugmentedLLM(
                agent=agent,
                nika_logger=logger,
                default_request_params=request_params,
            )
            await agent.attach_llm(llm=llm)
            report = await llm.generate_str(
                f"Task: {task_description}",
                request_params=request_params,
            )
            return report, llm.max_iterations_reached
