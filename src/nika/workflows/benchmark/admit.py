"""Benchmark trial resource classes for concurrent admission control.

Heavy labs contend on host-shared capacity even when CPU looks idle.
``resource_class`` + ``class_limits`` keep a global ``batch_size`` ceiling
while, with ``serialize_heavy``, exclusive classes run alone on the host.

Exclusive (one session, no peers under ``serialize_heavy``): Containerlab,
k8s/llmd/XRd, and any case with ``topo_size`` / ``topo`` ``l``.
"""

from __future__ import annotations

from typing import Any, Mapping

from nika.net_env.net_env_pool import list_all_net_envs
from nika.workflows.benchmark.trials import Trial

ResourceClass = str  # "clab" | "k8s" | "large" | "light"

# Scenarios that always use Containerlab even when ``backend`` is omitted.
CONTAINERLAB_ONLY_SCENARIOS = frozenset(
    name
    for name, spec in list_all_net_envs().items()
    if spec.supported_backends == ("containerlab",)
)

# Host-shared heavy labs that must not run two-at-a-time on one machine.
K8S_CLASS_SCENARIOS = frozenset(
    name for name, spec in list_all_net_envs().items() if spec.heavy_lab
)

CLASS_CLAB = "clab"
CLASS_K8S = "k8s"
CLASS_LARGE = "large"
CLASS_LIGHT = "light"

# While any of these are in flight, the host runs that session alone.
EXCLUSIVE_CLASSES = frozenset({CLASS_CLAB, CLASS_K8S, CLASS_LARGE})


def _topo_size(row: Mapping[str, Any]) -> str:
    return str(row.get("topo_size") or row.get("topo") or "").strip().lower()


def resource_class_for_row(row: Mapping[str, Any]) -> ResourceClass:
    """Classify a benchmark case row for admission control."""
    scenario = str(row.get("scenario") or "")
    backend = str(row.get("backend") or "")
    if backend == "containerlab" or scenario in CONTAINERLAB_ONLY_SCENARIOS:
        return CLASS_CLAB
    # case_key / task_id encodes backend for ISP containerlab rows.
    for key in ("case_key", "task_id"):
        token = str(row.get(key) or "")
        if "containerlab" in token:
            return CLASS_CLAB
    if scenario in K8S_CLASS_SCENARIOS:
        return CLASS_K8S
    if _topo_size(row) == "l":
        return CLASS_LARGE
    return CLASS_LIGHT


def resource_class(trial: Trial) -> ResourceClass:
    """Classify a ``Trial`` (uses ``trial.row`` plus ``trial.case_key``)."""
    row = dict(trial.row)
    row.setdefault("case_key", trial.case_key)
    return resource_class_for_row(row)


def class_limits(*, batch_size: int, serialize_heavy: bool) -> dict[ResourceClass, int]:
    """Per-class in-flight caps.

    When ``serialize_heavy`` is false, every class may use the full
    ``batch_size`` (flat sliding window). When true, exclusive classes are
    capped at 1; ``light`` still uses ``batch_size`` (global pool is the real
    ceiling). Exclusive classes additionally require an empty host (see
    ``can_admit``).
    """
    workers = max(1, int(batch_size))
    if not serialize_heavy:
        return {
            CLASS_CLAB: workers,
            CLASS_K8S: workers,
            CLASS_LARGE: workers,
            CLASS_LIGHT: workers,
        }
    return {
        CLASS_CLAB: 1,
        CLASS_K8S: 1,
        CLASS_LARGE: 1,
        CLASS_LIGHT: workers,
    }


def _in_flight_total(in_flight: Mapping[ResourceClass, int]) -> int:
    return sum(max(0, int(count)) for count in in_flight.values())


def _runs_exclusive(cls: ResourceClass, limits: Mapping[ResourceClass, int]) -> bool:
    # ``serialize_heavy`` caps exclusive classes at 1; flat limits do not.
    return cls in EXCLUSIVE_CLASSES and limits.get(cls, 1) == 1


def can_admit(
    cls: ResourceClass,
    *,
    in_flight: Mapping[ResourceClass, int],
    limits: Mapping[ResourceClass, int],
) -> bool:
    """Whether one more trial of ``cls`` may start given current in-flight counts."""
    if in_flight.get(cls, 0) >= limits.get(cls, 1):
        return False
    total = _in_flight_total(in_flight)
    if _runs_exclusive(cls, limits):
        # Exclusive labs need the whole host (no peer sessions of any class).
        return total == 0
    for exclusive in EXCLUSIVE_CLASSES:
        if _runs_exclusive(exclusive, limits) and in_flight.get(exclusive, 0) > 0:
            return False
    return True


def pick_admissible(
    pending: list[Trial],
    *,
    in_flight: Mapping[ResourceClass, int],
    limits: Mapping[ResourceClass, int],
) -> int | None:
    """Return the index of the next pending trial to start, or ``None``.

    Scans in order and admits the first trial that ``can_admit``. Once the
    scan reaches an exclusive trial that is waiting for the host to empty, it
    stops: admitting later light trials would keep the host busy and starve
    the exclusive trial until every light trial had run. The host drains, then
    the exclusive trial starts.
    """
    for index, trial in enumerate(pending):
        cls = resource_class(trial)
        if can_admit(cls, in_flight=in_flight, limits=limits):
            return index
        if _runs_exclusive(cls, limits):
            return None
    return None
