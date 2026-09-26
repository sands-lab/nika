"""Claude Agent SDK diagnosis phase."""

from agent.protocols import DIAGNOSIS
from agent.sdk.claude_sdk.worker import ClaudeSdkWorker
from agent.utils.skills import diagnosis_prompt_with_skills
from agent.utils.template import OVERALL_DIAGNOSIS_PROMPT


class ClaudeSdkDiagnosisPhase:
    def __init__(
        self,
        session_id: str,
        session_dir: str,
        model: str,
        max_steps: int = 20,
        scenario_name: str = "",
        *,
        llm_provider: str,
    ) -> None:
        self._worker = ClaudeSdkWorker(
            session_id=session_id,
            session_dir=session_dir,
            phase=DIAGNOSIS,
            model=model,
            llm_provider=llm_provider,
            max_steps=max_steps,
            scenario_name=scenario_name,
            system_prompt=diagnosis_prompt_with_skills(OVERALL_DIAGNOSIS_PROMPT),
        )

    async def run(self, task_description: str) -> str:
        return await self._worker.run(f"Task: {task_description}")
