"""OpenAI Codex SDK submission phase."""

from agent.protocols import SUBMISSION
from agent.sdk.codex_sdk.worker import CodexSdkWorker
from agent.utils.submission_context import submission_user_prompt
from agent.utils.template import SUBMIT_PROMPT_TEMPLATE


class CodexSdkSubmissionPhase:
    def __init__(
        self,
        session_id: str,
        session_dir: str,
        model: str = "gpt-5.4-mini",
        reasoning_effort: str | None = None,
        max_steps: int = 20,
        *,
        llm_provider: str,
        stream_output: bool = True,
    ) -> None:
        self._worker = CodexSdkWorker(
            session_id=session_id,
            session_dir=session_dir,
            phase=SUBMISSION,
            model=model,
            llm_provider=llm_provider,
            reasoning_effort=reasoning_effort,
            max_steps=max_steps,
            system_prompt=SUBMIT_PROMPT_TEMPLATE,
            stream_output=stream_output,
        )

    async def run(self, diagnosis_report: str, context: dict) -> str:
        return await self._worker.run(submission_user_prompt(diagnosis_report, context))
