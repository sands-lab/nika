"""Healthy (no-fault) benchmark case sentinel and helpers."""

from __future__ import annotations

import textwrap
from typing import Any

HEALTHY_PROBLEM = "healthy"


def is_healthy_case(problem: str | None) -> bool:
    return str(problem or "") == HEALTHY_PROBLEM


def healthy_task_description(net_env: Any) -> str:
    """Agent-facing prompt matching ``ProblemBase.get_task_description`` tone."""
    diagnostic_prompt = """\
        You are provided with the following network description and its current state:
        {net_desc}

        Your goal is to analyze the network condition and, if needed, use the available tools.
        You need to generate a troubeshooting diagnosis report.
        The report should reflect your assessment of the network's health, indicate any abnormal behavior you identify, and describe relevant findings based on your analysis.

        Focus on producing an informative and coherent diagnostic report derived from the network state.
        Do not need to propose any solutions or remediation steps at this stage.
        """
    tmpl = textwrap.dedent(diagnostic_prompt)
    return tmpl.format(net_desc=net_env.get_info()).strip()
