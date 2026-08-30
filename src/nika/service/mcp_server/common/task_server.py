import json
import os
from typing import Any, List

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, ValidationError

from nika.problems.prob_pool import (
    list_avail_problem_names as _list_avail_problems,
)
from nika.problems.root_cause import RootCause
from nika.service.mcp_server.session_context import get_session_dir, get_session_meta
from nika.utils.errors import safe_tool

mcp = FastMCP(
    "task_mcp_server",
    instructions=(
        "Task APIs: call list_resources() and list_avail_problems() to see the "
        "closed catalog, then submit() with chosen resource_id and fault_type pairs."
    ),
)


class SubmissionFormat(BaseModel):
    is_anomaly: bool = Field(
        ..., description="Indicates whether an anomaly was detected."
    )
    root_causes: List[dict] = Field(
        default_factory=list,
        description=(
            "Chosen diagnoses. Each item is {resource_id, fault_type} or "
            "{resource: {kind, node, name}, fault_type}. "
            "resource_id must match list_resources(); NIKA constructs it from "
            "resource fields when omitted. fault_type comes from "
            "list_avail_problems(). Example: "
            "[{'resource_id': 'interface/pc1/eth0', 'fault_type': 'link_down'}]"
        ),
    )
    faulty_devices: List[str] = Field(
        default_factory=list,
        description=(
            "Legacy device names. Use only when root_causes is empty. "
            "Example: ['router_1', 'switch_2']"
        ),
    )
    root_cause_name: List[str] = Field(
        default_factory=list,
        description=(
            "Legacy fault type names from list_avail_problems(). "
            "Use only when root_causes is empty."
        ),
    )


def _session_net_env():
    from nika.problems.topology_inventory import load_offline_net_env

    meta = get_session_meta()
    scenario = str(meta.get("scenario_name") or "")
    params = meta.get("scenario_params") or {}
    topo = (
        meta.get("scenario_topo_size")
        or params.get("topo_size")
        or params.get("size")
        or ""
    )
    if topo in ("-", None):
        topo = ""
    return load_offline_net_env(scenario, str(topo))


def _live_k8s_catalog() -> tuple[list[dict], list[dict]]:
    try:
        from nika.service.k8s_mcp_server.client import get_client

        client = get_client()
        services = client.list_services(all_namespaces=True)
        policies = client.get_network_policies(all_namespaces=True)
        if not isinstance(services, list):
            services = []
        if not isinstance(policies, list):
            policies = []
        return services, policies
    except Exception:
        return [], []


def catalog_resource_ids() -> list[str]:
    from nika.problems.topology_inventory import catalog_resources

    services, policies = _live_k8s_catalog()
    items = catalog_resources(
        _session_net_env(),
        k8s_services=services,
        k8s_network_policies=policies,
    )
    return [item.id for item in items]


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
        try:
            cause = RootCause.model_validate(raw)
        except (ValidationError, ValueError) as exc:
            errors.append(f"root_causes[{index}]: {exc}")
            continue
        resource_id = cause.resource_id or ""
        if resource_id not in catalog_ids:
            errors.append(
                f"root_causes[{index}]: resource_id {resource_id!r} is not in "
                "list_resources(). Call list_resources() and pick an id."
            )
        if cause.fault_type not in fault_types:
            errors.append(
                f"root_causes[{index}]: fault_type {cause.fault_type!r} is not in "
                "list_avail_problems()."
            )
        parsed.append({"resource_id": resource_id, "fault_type": cause.fault_type})
    return parsed, errors


@safe_tool
@mcp.tool()
def list_avail_problems() -> list[str]:
    """List all available fault types (failure IDs) you may submit.

    Returns:
        list[str]: Fault type names such as link_down or bgp_asn_misconfig.
    """
    return _list_avail_problems()


@safe_tool
@mcp.tool()
def list_resources() -> list[dict[str, str]]:
    """List lab-enumerable resources you may submit as localization targets.

    Returns:
        list[dict]: Each item has id and kind (node, interface, or k8s).
    """
    from nika.problems.topology_inventory import catalog_resources

    services, policies = _live_k8s_catalog()
    items = catalog_resources(
        _session_net_env(),
        k8s_services=services,
        k8s_network_policies=policies,
    )
    return [{"id": item.id, "kind": str(item.kind)} for item in items]


@safe_tool
@mcp.tool()
def submit(
    is_anomaly: bool,
    root_causes: List[dict] | None = None,
    faulty_devices: List[str] | None = None,
    root_cause_name: List[str] | None = None,
) -> List[str]:
    """Submit the diagnosis. Prefer resource_id + fault_type pairs from the list tools.

    Args:
        is_anomaly: Whether an anomaly was detected.
        root_causes: Diagnoses as [{resource_id, fault_type}, ...] from
            list_resources and list_avail_problems, or the same pair with
            resource fields instead of resource_id. NIKA constructs resource_id.
        faulty_devices: Legacy device names. Accepted only when root_causes is empty.
        root_cause_name: Legacy fault types. Accepted only when root_causes is empty.
    """
    causes = list(root_causes or [])
    if causes:
        parsed, errors = validate_root_cause_choices(
            causes,
            catalog_ids=set(catalog_resource_ids()),
            fault_types=set(_list_avail_problems()),
        )
        if errors:
            return ["Submission rejected: " + " ".join(errors)]
        causes = parsed

    submission_dict: dict[str, Any] = {
        "is_anomaly": is_anomaly,
        "root_causes": causes,
        "faulty_devices": list(faulty_devices or []),
        "root_cause_name": list(root_cause_name or []),
    }
    session_dir = get_session_dir()
    os.makedirs(session_dir, exist_ok=True)
    submission_path = os.path.join(session_dir, "submission.json")
    with open(submission_path, "w+", encoding="utf-8") as log_file:
        log_file.write(json.dumps(submission_dict))

    return ["Submission success."]


if __name__ == "__main__":
    mcp.run(transport="stdio")
