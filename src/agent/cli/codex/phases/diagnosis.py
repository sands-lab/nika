"""Codex CLI-backed diagnosis phase worker.

Mirrors the role of :class:`~agent.byo.langgraph.phases.DiagnosisPhase`
in the LangChain path: wraps the same troubleshooting prompt and exposes an
async ``run()`` that returns a free-text diagnosis report.
"""

from agent.cli.codex.codex_worker import CodexWorker
from agent.utils.skills import diagnosis_prompt_with_skills
from agent.utils.template import OVERALL_DIAGNOSIS_PROMPT
from agent.protocols import DIAGNOSIS


class CodexCliDiagnosisPhase:
    """Runs network fault diagnosis via a ``codex exec`` subprocess.

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
    scenario_name:
        Scenario identifier used to select relevant Kathara MCP servers.
    """

    def __init__(
        self,
        session_id: str,
        session_dir: str,
        model: str = "gpt-5.4-mini",
        reasoning_effort: str | None = None,
        max_steps: int = 20,
        scenario_name: str = "",
        *,
        llm_provider: str,
        stream_output: bool = True,
        trace_dir: str | None = None,
    ) -> None:
        self._worker = CodexWorker(
            session_id=session_id,
            session_dir=session_dir,
            phase=DIAGNOSIS,
            model=model,
            reasoning_effort=reasoning_effort,
            max_steps=max_steps,
            scenario_name=scenario_name,
            llm_provider=llm_provider,
            stream_output=stream_output,
            trace_dir=trace_dir,
        )
        self._diagnosis_prompt = diagnosis_prompt_with_skills(OVERALL_DIAGNOSIS_PROMPT)

    async def run(self, task_description: str) -> str:
        """Return a free-text diagnosis report produced by ``codex exec``."""
        prompt = f"{self._diagnosis_prompt}\n\nTask: {task_description}"
        return await self._worker.run(prompt)
