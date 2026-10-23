"""DeepSeek Harness CLI-backed submission phase worker.

Calls the task MCP server's ``submit`` tool to record a structured result
based on the diagnosis report.
"""

from agent.cli.dsh.dsh_worker import DshWorker
from agent.protocols import SUBMISSION
from agent.utils.submission_context import submission_single_prompt


class DshSubmissionPhase:
    """Calls the task MCP server's ``submit`` tool via headless ``dsh``.

    Parameters
    ----------
    session_id:
        Opaque session handle of the run.
    session_dir:
        Absolute path to the session results directory.
    model:
        Model id (e.g. ``"deepseek-v4-flash"``).
    llm_provider:
        Active LLM provider forwarded to the dsh worker.
    max_steps:
        LLM-turn budget for the phase (enforced by the worker).
    """

    def __init__(
        self,
        session_id: str,
        session_dir: str,
        model: str | None = None,
        max_steps: int = 20,
        *,
        llm_provider: str,
        stream_output: bool = True,
        trace_dir: str | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._worker = DshWorker(
            session_id=session_id,
            session_dir=session_dir,
            phase=SUBMISSION,
            model=model,
            max_steps=max_steps,
            llm_provider=llm_provider,
            stream_output=stream_output,
            trace_dir=trace_dir,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )

    async def run(self, diagnosis_report: str, context: dict) -> str:
        """Submit the frozen diagnosis report via the task MCP server."""
        return await self._worker.run(
            submission_single_prompt(diagnosis_report, context)
        )
