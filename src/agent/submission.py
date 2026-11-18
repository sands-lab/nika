"""Structured submission with the agent's own model.

Agents that do not override :meth:`~agent.base.DiagnosisAgent.submit` get
:func:`submit_with_agent_model`: a LangGraph ReAct agent with only the task MCP
``submit`` tool, the same worker ``byo.langgraph`` uses. It sees only the
frozen report, the fault ontology, and the resource catalog.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.base import DiagnosisAgent


async def submit_with_agent_model(
    agent: DiagnosisAgent,
    diagnosis_report: str,
    context: dict[str, Any],
) -> str:
    """Structured submission with the agent's model and ``max_steps`` budget."""
    if not agent.llm_provider or not agent.model:
        raise RuntimeError(
            "The default submission uses the agent's model: the run needs a "
            "provider and a model, or the agent must override submit()."
        )
    from agent.byo.langgraph.phases.submission import SubmissionPhase

    phase = SubmissionPhase(
        llm_provider=agent.llm_provider,
        model=agent.model,
        reasoning_effort=agent.reasoning_effort,
        max_tokens=agent.max_tokens,
        max_steps=agent.max_steps,
    )
    return await phase.run(diagnosis_report, context, trace_dir=agent.trace_dir)
