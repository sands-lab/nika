"""Claude Agent SDK submission phase."""

from agent.protocols import SUBMISSION
from agent.sdk.claude_sdk.worker import ClaudeSdkWorker
from agent.utils.submission_context import submission_user_prompt
from agent.utils.template import SUBMIT_PROMPT_TEMPLATE


class ClaudeSdkSubmissionPhase:
    def __init__(
        self,
        session_id: str,
        session_dir: str,
        model: str,
        max_steps: int = 20,
        *,
        llm_provider: str,
    ) -> None:
        self._worker = ClaudeSdkWorker(
            session_id=session_id,
            session_dir=session_dir,
            phase=SUBMISSION,
            model=model,
            llm_provider=llm_provider,
            max_steps=max_steps,
            system_prompt=SUBMIT_PROMPT_TEMPLATE,
        )

    async def run(self, diagnosis_report: str, context: dict) -> str:
        return await self._worker.run(submission_user_prompt(diagnosis_report, context))
