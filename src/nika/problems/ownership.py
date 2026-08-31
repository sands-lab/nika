"""Canonical owner-kind policy for fault types used by submissions."""

from __future__ import annotations

from nika.problems.base import FailureDomain
from nika.problems.registry import get_problem_class

_LINK_OWNED_FAULTS = frozenset(
    {
        "link_down",
        "link_flap",
        "link_packet_corruption",
        "link_capacity_bottleneck",
    }
)

# traffic_queueing_resource defaults to interface ownership; these end-host
# stack faults label the mutated node instead.
_NODE_OWNED_TRAFFIC_FAULTS = frozenset(
    {
        "tcp_receive_window_limited",
    }
)

_DOMAIN_OWNER = {
    FailureDomain.LINK_INTERFACE.value: "interface",
    FailureDomain.TRAFFIC_QUEUEING_RESOURCE.value: "interface",
}


def owner_kind_for_fault(fault_type: str) -> str:
    """Return the canonical owner kind used by ground truth and submissions.

    Controller-side cable faults (``link_down``, ``link_flap``,
    ``link_packet_corruption``, ``link_capacity_bottleneck``) are owned by
    the undirected link TP set.
    Other ``link_interface`` and ``traffic_queueing_resource`` faults remain
    interface-owned unless listed in ``_NODE_OWNED_TRAFFIC_FAULTS``.
    Remaining failures use their concrete mutated node/k8s resource.
    """
    cls = get_problem_class(fault_type)
    if cls is None:
        raise KeyError(f"Unknown fault type: {fault_type!r}")
    if fault_type in _LINK_OWNED_FAULTS:
        return "link"
    if fault_type in _NODE_OWNED_TRAFFIC_FAULTS:
        return "node_or_k8s"
    domain = str(getattr(cls, "failure_domain", ""))
    return _DOMAIN_OWNER.get(domain, "node_or_k8s")


def ownership_entries(fault_types: list[str]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for fault_type in sorted(set(fault_types)):
        cls = get_problem_class(fault_type)
        if cls is None:
            raise KeyError(f"Unknown fault type: {fault_type!r}")
        # Prefer the explicit class description only. Do not fall back to
        # symptom_desc / META text, which can leak probe differentials.
        description = (getattr(cls, "description", None) or "").strip()
        entries.append(
            {
                "id": fault_type,
                "description": description or fault_type,
                "owner_kind": owner_kind_for_fault(fault_type),
            }
        )
    return entries
