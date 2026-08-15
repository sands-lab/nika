"""OpenAI Codex SDK diagnosis phase."""

from agent.sdk.codex_sdk.worker import CodexSdkWorker
from agent.utils.skills import diagnosis_prompt_with_skills
from agent.utils.template import OVERALL_DIAGNOSIS_PROMPT
from agent.protocols import DIAGNOSIS


class CodexSdkDiagnosisPhase:
    def __init__(
        self,
        session_id: str,
        session_dir: str,
        model: str = "gpt-5.4-mini",
        reasoning_effort: str | None = None,
        scenario_name: str = "",
        *,
        llm_provider: str,
        stream_output: bool = True,
    ) -> None:
        diagnosis_prompt = diagnosis_prompt_with_skills(OVERALL_DIAGNOSIS_PROMPT)
        self._diagnosis_prompt = diagnosis_prompt
        self._worker = CodexSdkWorker(
            session_id=session_id,
            session_dir=session_dir,
            phase=DIAGNOSIS,
            model=model,
            llm_provider=llm_provider,
            reasoning_effort=reasoning_effort,
            scenario_name=scenario_name,
            system_prompt=diagnosis_prompt,
            stream_output=stream_output,
        )

    async def run(self, task_description: str) -> str:
        prompt = f"{self._diagnosis_prompt}\n\nTask: {task_description}"
        return await self._worker.run(prompt)
