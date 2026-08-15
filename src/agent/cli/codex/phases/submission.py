"""Codex CLI-backed submission phase worker.

Mirrors the role of :class:`~agent.byo.langgraph.phases.SubmissionPhase`
in the LangChain path: calls the task MCP server's ``submit`` tool to record
a structured result based on the diagnosis report.
"""

from agent.cli.codex.codex_worker import CodexWorker
from agent.utils.template import SUBMIT_PROMPT_TEMPLATE
from agent.protocols import SUBMISSION


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
    timeout:
        Hard timeout in seconds for the subprocess.
    """

    def __init__(
        self,
        session_id: str,
        session_dir: str,
        model: str = "gpt-5.4-mini",
        reasoning_effort: str | None = None,
        timeout: int = 300,
        *,
        llm_provider: str,
        stream_output: bool = True,
    ) -> None:
        self._worker = CodexWorker(
            session_id=session_id,
            session_dir=session_dir,
            phase=SUBMISSION,
            model=model,
            reasoning_effort=reasoning_effort,
            timeout=timeout,
            llm_provider=llm_provider,
            stream_output=stream_output,
        )

    async def run(self, diagnosis_report: str) -> str:
        """Submit the diagnosis result via the task MCP server.

        Parameters
        ----------
        diagnosis_report:
            Free-text output from the diagnosis phase.  Forwarded verbatim
            to the Codex CLI so it can extract the structured answer and call
            ``submit()``.
        """
        prompt = (
            f"{SUBMIT_PROMPT_TEMPLATE}\n\n"
            f"Based on the diagnosis report: {diagnosis_report}\n"
            "Please provide the submission. Do not submit if no report is available."
        )
        return await self._worker.run(prompt)
