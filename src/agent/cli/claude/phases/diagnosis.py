"""Claude Code CLI-backed diagnosis phase worker.

Mirrors the role of :class:`~agent.byo.langgraph.phases.DiagnosisPhase`
in the LangChain path: wraps the same troubleshooting prompt and exposes an
async ``run()`` that returns a free-text diagnosis report.
"""

from agent.cli.claude.claude_worker import ClaudeWorker
from agent.utils.skills import diagnosis_prompt_with_skills
from agent.utils.template import OVERALL_DIAGNOSIS_PROMPT
from agent.protocols import DIAGNOSIS


class ClaudeDiagnosisPhase:
    """Runs network fault diagnosis via a ``claude -p`` subprocess.

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
    scenario_name:
        Scenario identifier used to select relevant Kathara MCP servers.
    """

    def __init__(
        self,
        session_id: str,
        session_dir: str,
        model: str | None = None,
        timeout: int = 600,
        scenario_name: str = "",
        *,
        stream_output: bool = True,
    ) -> None:
        self._worker = ClaudeWorker(
            session_id=session_id,
            session_dir=session_dir,
            phase=DIAGNOSIS,
            model=model,
            timeout=timeout,
            scenario_name=scenario_name,
            stream_output=stream_output,
        )
        self._diagnosis_prompt = diagnosis_prompt_with_skills(OVERALL_DIAGNOSIS_PROMPT)

    async def run(self, task_description: str) -> str:
        """Return a free-text diagnosis report produced by ``claude -p``."""
        prompt = f"{self._diagnosis_prompt}\n\nTask: {task_description}"
        return await self._worker.run(prompt)
