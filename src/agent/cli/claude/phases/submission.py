"""Claude Code CLI-backed submission phase worker.

Mirrors the role of :class:`~agent.byo.langgraph.phases.SubmissionPhase`
in the LangChain path: calls the task MCP server's ``submit`` tool to record
a structured result based on the diagnosis report.
"""

from agent.cli.claude.claude_worker import ClaudeWorker
from agent.utils.template import SUBMIT_PROMPT_TEMPLATE
from agent.utils.phases import SUBMISSION


class ClaudeSubmissionPhase:
    """Calls the task MCP server's ``submit`` tool via a ``claude -p`` subprocess.

    Parameters
    ----------
    session_id:
        NIKA session identifier.
    session_dir:
        Absolute path to the session results directory.
    model:
        Claude/DeepSeek model name (e.g. ``"deepseek-v4-flash"``).
    timeout:
        Hard timeout in seconds for the subprocess.
    """

    def __init__(
        self,
        session_id: str,
        session_dir: str,
        model: str | None = None,
        timeout: int = 300,
        *,
        stream_output: bool = True,
    ) -> None:
        self._worker = ClaudeWorker(
            session_id=session_id,
            session_dir=session_dir,
            phase=SUBMISSION,
            model=model,
            timeout=timeout,
            stream_output=stream_output,
        )

    async def run(self, diagnosis_report: str) -> str:
        """Submit the diagnosis result via the task MCP server.

        Parameters
        ----------
        diagnosis_report:
            Free-text output from the diagnosis phase.  Forwarded verbatim
            to Claude so it can extract the structured answer and call
            ``submit()``.
        """
        prompt = (
            f"{SUBMIT_PROMPT_TEMPLATE}\n\n"
            f"Based on the diagnosis report: {diagnosis_report}\n"
            "Please provide the submission. Do not submit if no report is available."
        )
        return await self._worker.run(prompt)
