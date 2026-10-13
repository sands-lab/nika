"""Codex CLI-backed submission phase worker.

Mirrors the role of :class:`~agent.byo.langgraph.phases.SubmissionPhase`
in the LangChain path: calls the task MCP server's ``submit`` tool to record
a structured result based on the diagnosis report.
"""

from agent.cli.codex.codex_worker import CodexWorker
from agent.protocols import SUBMISSION
from agent.utils.submission_context import submission_single_prompt


class CodexCliSubmissionPhase:
    """Calls the task MCP server's ``submit`` tool via a ``codex exec`` subprocess.

    Parameters
    ----------
    session_id:
        NIKA session identifier.
    session_dir:
        Absolute path to the session results directory.
    model:
        Codex model name (e.g. ``"gpt-5.4-mini"``).
    llm_provider:
        Active LLM provider forwarded to the Codex worker.
    reasoning_effort:
        Optional Codex ``model_reasoning_effort`` override.
    max_steps:
        LLM-turn budget for the phase (enforced by the worker).
    """

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
        trace_dir: str | None = None,
    ) -> None:
        self._worker = CodexWorker(
            session_id=session_id,
            session_dir=session_dir,
            phase=SUBMISSION,
            model=model,
            reasoning_effort=reasoning_effort,
            max_steps=max_steps,
            llm_provider=llm_provider,
            stream_output=stream_output,
            trace_dir=trace_dir,
        )

    async def run(self, diagnosis_report: str, context: dict) -> str:
        """Submit the frozen diagnosis report via the task MCP server."""
        return await self._worker.run(
            submission_single_prompt(diagnosis_report, context)
        )
