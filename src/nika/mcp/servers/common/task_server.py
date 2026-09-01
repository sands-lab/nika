import json
import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from nika.mcp.session_context import get_session_dir
from nika.problems.rca import RootCause
from nika.utils.errors import safe_tool

mcp = FastMCP(
    "task_mcp_server",
    instructions=(
        "Task API: submit() exactly once using the frozen diagnosis report, fault "
        "ontology, and canonical resource inventory supplied in your prompt."
    ),
)


def _submission_catalog() -> tuple[set[str], set[str], str]:
    from nika.mcp.session_context import require_session_id
    from nika.workflows.agent.submission import load_submission_context

    context = load_submission_context(require_session_id())
    resources = {
        str(item.get("id"))
        for item in context.get("resources") or []
        if isinstance(item, dict) and item.get("id")
    }
    return (
        resources,
        {
            str(item.get("id")) if isinstance(item, dict) else str(item)
            for item in context.get("fault_ontology") or []
        },
        str(context["diagnosis_report"]),
    )


def validate_root_cause_choices(
    root_causes: list[dict],
    *,
    catalog_ids: set[str],
    fault_types: set[str],
) -> tuple[list[dict], list[str]]:
    """Return (canonical causes, errors). Empty errors means accepted."""
    parsed: list[dict] = []
    errors: list[str] = []
    for index, raw in enumerate(root_causes):
        if not isinstance(raw, dict):
            errors.append(f"root_causes[{index}]: must be an object")
            continue
        unknown = set(raw) - {"resource", "resource_id", "fault_type"}
        if unknown:
            errors.append(f"root_causes[{index}]: unknown fields {sorted(unknown)!r}")
            continue
        try:
            cause = RootCause.model_validate(raw)
        except (ValidationError, ValueError) as exc:
            errors.append(f"root_causes[{index}]: {exc}")
            continue
        resource_id = cause.resource_id or ""
        if resource_id not in catalog_ids:
            errors.append(
                f"root_causes[{index}]: resource_id {resource_id!r} is not in "
                "the supplied canonical resource inventory."
            )
        if cause.fault_type not in fault_types:
            errors.append(
                f"root_causes[{index}]: fault_type {cause.fault_type!r} is not in "
                "the supplied fault ontology."
            )
        parsed.append({"resource_id": resource_id, "fault_type": cause.fault_type})
    return parsed, errors


@safe_tool
@mcp.tool()
def submit(
    is_anomaly: bool,
    root_causes: list[dict] | None = None,
) -> list[str]:
    """Submit the diagnosis as resource_id + fault_type pairs from frozen context.

    Args:
        is_anomaly: Whether an anomaly was detected.
        root_causes: Diagnoses as [{resource_id, fault_type}, ...] selected
            from the prompt's canonical resource inventory and fault ontology.
    """
    if type(is_anomaly) is not bool:
        return ["Submission rejected: is_anomaly must be a boolean."]
    causes = list(root_causes or [])
    if not is_anomaly and causes:
        return [
            "Submission rejected: healthy/no-fault submissions require root_causes=[]."
        ]
    if is_anomaly and not causes:
        return ["Submission rejected: anomalous submissions require root_causes."]
    catalog_ids, fault_types, diagnosis_report = _submission_catalog()
    if causes:
        parsed, errors = validate_root_cause_choices(
            causes,
            catalog_ids=catalog_ids,
            fault_types=fault_types,
        )
        if errors:
            return ["Submission rejected: " + " ".join(errors)]
        keys = [(item["resource_id"], item["fault_type"]) for item in parsed]
        if len(set(keys)) != len(keys):
            return ["Submission rejected: duplicate root cause pairs."]
        causes = [
            {"resource_id": resource_id, "fault_type": fault_type}
            for resource_id, fault_type in sorted(keys)
        ]

    submission_dict: dict[str, Any] = {
        "diagnosis_report": diagnosis_report,
        "is_anomaly": is_anomaly,
        "root_causes": causes,
    }
    session_dir = get_session_dir()
    os.makedirs(session_dir, exist_ok=True)
    submission_path = os.path.join(session_dir, "submission.json")
    if os.path.exists(submission_path):
        return ["Submission rejected: a canonical submission already exists."]
    with open(submission_path, "x", encoding="utf-8") as log_file:
        log_file.write(json.dumps(submission_dict))

    return ["Submission success."]


if __name__ == "__main__":
    mcp.run(transport="stdio")
