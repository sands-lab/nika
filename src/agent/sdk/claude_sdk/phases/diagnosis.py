"""Claude Agent SDK diagnosis phase."""

from agent.sdk.claude_sdk.worker import ClaudeSdkWorker
from agent.utils.skills import diagnosis_prompt_with_skills
from agent.utils.template import OVERALL_DIAGNOSIS_PROMPT
from agent.protocols import DIAGNOSIS


class ClaudeSdkDiagnosisPhase:
    def __init__(
        self,
        session_id: str,
        session_dir: str,
        model: str,
        max_steps: int = 20,
        scenario_name: str = "",
    ) -> None:
        diagnosis_prompt = diagnosis_prompt_with_skills(OVERALL_DIAGNOSIS_PROMPT)
        self._diagnosis_prompt = diagnosis_prompt
        self._worker = ClaudeSdkWorker(
            session_id=session_id,
            session_dir=session_dir,
            phase=DIAGNOSIS,
            model=model,
            max_steps=max_steps,
            scenario_name=scenario_name,
            system_prompt=diagnosis_prompt,
        )

    async def run(self, task_description: str) -> str:
        prompt = f"{self._diagnosis_prompt}\n\nTask: {task_description}"
        return await self._worker.run(prompt)
