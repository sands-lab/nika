"""Freeze diagnosis and derive the final submission prompt context."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.protocols import DIAGNOSIS
from agent.utils.loggers import MESSAGES_FILENAME
from nika.problems.ownership import ownership_entries
from nika.problems.registry import list_avail_problem_names
from nika.problems.rca.inventory import catalog_resources, load_offline_net_env
from nika.mcp.gateway.session_registry import get_session
from nika.utils.session_store import SessionStore
from nika.workflows.benchmark.healthy import is_healthy_case


def _case_ontology(row: dict[str, Any]) -> list[str]:
    metadata = row.get("metadata") or {}
    values = row.get("fault_ontology") or metadata.get("fault_ontology")
    if isinstance(values, list) and all(isinstance(value, str) for value in values):
        names = values
    else:
        names = list_avail_problem_names()
    # ``healthy`` is a benchmark sentinel, not a registered fault type.
    return sorted({name for name in names if not is_healthy_case(name)})


def _trajectory_path(session_id: str) -> Path:
    entry = get_session(session_id)
    if entry is None:
        raise KeyError(f"MCP gateway session not registered: {session_id!r}")
    return Path(entry.session_dir) / MESSAGES_FILENAME


def _frozen_report(path: Path) -> str | None:
    if not path.is_file():
        return None
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == "diagnosis_frozen":
            report = event.get("report")
            return report if isinstance(report, str) else ""
    return None


def freeze_diagnosis(session_id: str, report: str) -> dict[str, str]:
    """Append the immutable final report to the diagnosis trajectory once."""
    path = _trajectory_path(session_id)
    existing = _frozen_report(path)
    if existing is not None:
        return {"report": existing}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": datetime.now().isoformat(),
                    "phase": DIAGNOSIS,
                    "event": "diagnosis_frozen",
                    "report": report,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return {"report": report}


def load_submission_context(session_id: str) -> dict[str, Any]:
    """Build prompt-only context from immutable trajectory and scenario metadata."""
    report = _frozen_report(_trajectory_path(session_id))
    if report is None:
        raise RuntimeError("Diagnosis must be frozen before submission.")
    row = SessionStore().get_session(session_id)
    params = row.get("scenario_params") or {}
    env = load_offline_net_env(
        str(row.get("scenario_name") or ""),
        str(row.get("scenario_topo_size") or params.get("topo_size") or ""),
        topo=params.get("topo"),
        igp=params.get("igp"),
        bgp_mode=params.get("bgp_mode"),
    )
    return {
        "diagnosis_report": report,
        "fault_ontology": ownership_entries(_case_ontology(row)),
        "resources": [
            {"id": item.id, "kind": str(item.kind)} for item in catalog_resources(env)
        ],
    }
