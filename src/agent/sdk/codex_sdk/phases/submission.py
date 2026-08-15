"""OpenAI Codex SDK submission phase."""

from agent.sdk.codex_sdk.worker import CodexSdkWorker
from agent.utils.template import SUBMIT_PROMPT_TEMPLATE
from agent.protocols import SUBMISSION


class CodexSdkSubmissionPhase:
    def __init__(
        self,
        session_id: str,
        session_dir: str,
        model: str = "gpt-5.4-mini",
        reasoning_effort: str | None = None,
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
            system_prompt=SUBMIT_PROMPT_TEMPLATE,
            stream_output=stream_output,
        )

    async def run(self, diagnosis_report: str) -> str:
        prompt = (
            f"{SUBMIT_PROMPT_TEMPLATE}\n\n"
            f"Based on the diagnosis report: {diagnosis_report}\n"
            "Please provide the submission. Do not submit if no report is available."
        )
        return await self._worker.run(prompt)
