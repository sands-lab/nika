"""Claude Agent SDK submission phase."""

from agent.sdk.claude_sdk.worker import ClaudeSdkWorker
from agent.utils.template import SUBMIT_PROMPT_TEMPLATE
from agent.protocols import SUBMISSION


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

    async def run(self, diagnosis_report: str) -> str:
        prompt = (
            f"{SUBMIT_PROMPT_TEMPLATE}\n\n"
            f"Based on the diagnosis report: {diagnosis_report}\n"
            "Please provide the submission. Do not submit if no report is available."
        )
        return await self._worker.run(prompt)
