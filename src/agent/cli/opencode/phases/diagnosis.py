"""OpenCode CLI-backed diagnosis phase worker.

Mirrors the role of :class:`~agent.byo.langgraph.phases.DiagnosisPhase`
in the LangChain path: wraps the same troubleshooting prompt and exposes an
async ``run()`` that returns a free-text diagnosis report.
"""

from agent.cli.opencode.opencode_worker import OpenCodeWorker
from agent.utils.skills import diagnosis_prompt_with_skills
from agent.utils.template import OVERALL_DIAGNOSIS_PROMPT
from agent.protocols import DIAGNOSIS


class OpenCodeDiagnosisPhase:
    """Runs network fault diagnosis via an ``opencode run`` subprocess.

    Parameters
    ----------
    session_id:
        Opaque session handle of the run.
    session_dir:
        Absolute path to the session results directory.
    model:
        Model name forwarded as ``--model endpoint/<model>``.
    llm_provider:
        Active LLM provider forwarded to the OpenCode worker.
    max_steps:
        LLM-turn budget for the phase (enforced by the worker).
    scenario_name:
        Scenario identifier used to select relevant diagnosis MCP servers.
    """

    def __init__(
        self,
        session_id: str,
        session_dir: str,
        model: str | None = None,
        max_steps: int = 20,
        scenario_name: str = "",
        *,
        llm_provider: str,
        stream_output: bool = True,
        trace_dir: str | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self._worker = OpenCodeWorker(
            session_id=session_id,
            session_dir=session_dir,
            phase=DIAGNOSIS,
            model=model,
            max_steps=max_steps,
            scenario_name=scenario_name,
            llm_provider=llm_provider,
            stream_output=stream_output,
            trace_dir=trace_dir,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
        self._diagnosis_prompt = diagnosis_prompt_with_skills(OVERALL_DIAGNOSIS_PROMPT)

    async def run(self, task_description: str) -> str:
        """Return a free-text diagnosis report produced by ``opencode run``."""
        prompt = f"{self._diagnosis_prompt}\n\nTask: {task_description}"
        return await self._worker.run(prompt)
