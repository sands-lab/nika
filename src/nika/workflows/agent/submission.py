"""Freeze diagnosis and derive the final submission prompt context."""

from __future__ import annotations

import json
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from agent.protocols import DIAGNOSIS
from agent.utils.loggers import MESSAGES_FILENAME
from nika.problems.ownership import ownership_entries
from nika.problems.registry import list_avail_problem_names
from nika.problems.rca.inventory import (
    catalog_resources,
    load_session_offline_net_env,
)
from nika.mcp.gateway.session_registry import get_session
from nika.utils.session_store import SessionStore
from nika.workflows.benchmark.healthy import is_healthy_case


def fault_candidates() -> list[str]:
    """Fixed fault-type candidates: every registered fault except ``healthy``.

    The same list is offered in every launch mode (release, ``--config``,
    single case) so the candidate set never depends on which cases were run.
    """
    # ``healthy`` is a benchmark sentinel, not a registered fault type.
    return sorted(
        {name for name in list_avail_problem_names() if not is_healthy_case(name)}
    )


def _trajectory_path(session_id: str) -> Path:
    entry = get_session(session_id)
    if entry is None:
        raise KeyError("MCP gateway session not registered")
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
                    "timestamp": datetime.now(UTC).isoformat(),
                    "phase": DIAGNOSIS,
                    "event": "diagnosis_frozen",
                    "report": report,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return {"report": report}


def load_frozen_diagnosis_report(session_id: str) -> str | None:
    """Return the frozen diagnosis report, or ``None`` if not yet frozen."""
    return _frozen_report(_trajectory_path(session_id))


def load_submission_catalog(session_id: str) -> dict[str, Any]:
    """Fault ontology + resources for the submission prompt (no freeze required)."""
    row = SessionStore().get_session(session_id)
    env = load_session_offline_net_env(row)
    k8s_services, k8s_network_policies = _live_k8s_objects(row)
    resources = catalog_resources(
        env,
        k8s_services=k8s_services,
        k8s_network_policies=k8s_network_policies,
    )
    return {
        "fault_ontology": ownership_entries(fault_candidates()),
        "resources": [{"id": item.id, "kind": str(item.kind)} for item in resources],
    }


def _live_k8s_objects(row: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """All Services and NetworkPolicies of a k8s session's cluster.

    k8s ground truth names cluster objects, so they must be submittable; list
    every object (not just GT ones) to keep the catalog answer-neutral.
    """
    from nika.mcp.k8s.client import K8sClient, resolve_kubeconfig_path

    try:
        kubeconfig = resolve_kubeconfig_path(row)
    except FileNotFoundError:
        return [], []  # Not a k8s session.
    k8s = K8sClient(kubeconfig=kubeconfig)
    return (
        k8s.list_services(all_namespaces=True),
        k8s.get_network_policies(all_namespaces=True),
    )


def load_submission_context(session_id: str) -> dict[str, Any]:
    """Build prompt-only context from immutable trajectory and scenario metadata."""
    report = load_frozen_diagnosis_report(session_id)
    if report is None:
        raise RuntimeError("Diagnosis must be frozen before submission.")
    catalog = load_submission_catalog(session_id)
    return {
        "diagnosis_report": report,
        "fault_ontology": catalog["fault_ontology"],
        "resources": catalog["resources"],
    }
