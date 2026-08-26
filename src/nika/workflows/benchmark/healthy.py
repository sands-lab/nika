"""Healthy (no-fault) benchmark case sentinel and helpers."""

from __future__ import annotations

import textwrap
from typing import Any

HEALTHY_PROBLEM = "healthy"

# Fixed ISP deploy profile for healthy ISP cases (must be self-contained).
HEALTHY_ISP_OPTIONS: dict[str, Any] = {
    "topo": "abilene",
    "igp": "ospf",
    "bgp_mode": "ebgp",
    "rpki": False,
}

# One healthy case per scenario present in the selected matrix.
SELECTED_HEALTHY_SCENARIOS: tuple[str, ...] = (
    "campus_lan",
    "dc_clos",
    "enterprise_branch",
    "isp",
    "p4_dc_fabric",
    "p4_dc_gateway",
    "sdn_l3_clos",
)


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


def write_healthy_session_artifacts(session_id: str) -> None:
    """Write healthy ground truth and task description for a deployed session."""
    from nika.problems.rca import healthy_ground_truth
    from nika.problems.rca.inventory import load_offline_net_env
    from nika.utils.session import Session

    session = Session().load_running_session(session_id=session_id)
    params = dict(getattr(session, "scenario_params", None) or {})
    topo_size = (
        getattr(session, "scenario_topo_size", None) or params.get("topo_size") or ""
    )
    isp_kwargs: dict[str, Any] = {}
    for key in ("topo", "igp", "bgp_mode", "rpki"):
        if key in params and params[key] is not None:
            isp_kwargs[key] = params[key]
    net_env = load_offline_net_env(
        str(session.scenario_name),
        str(topo_size or ""),
        **isp_kwargs,
    )
    session.write_gt(healthy_ground_truth().model_dump(mode="json", exclude_none=True))
    session.update_session("task_description", healthy_task_description(net_env))
    session.update_session("problem_names", [])
