"""Benchmark trial resource classes for concurrent admission control.

Heavy labs contend on host-shared capacity even when CPU looks idle.
``resource_class`` + ``tier_limits`` split trials into two tiers: light trials
run up to ``batch_size`` at once, heavy trials up to ``heavy_batch_size``, and
the two tiers never share the host. ``heavy_batch_size=1`` runs each heavy
trial alone.

Heavy: Containerlab, k8s/llmd/XRd, and any case with ``topo_size`` /
``topo`` ``l``.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

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

HEAVY_CLASSES = frozenset({CLASS_CLAB, CLASS_K8S, CLASS_LARGE})

TIER_HEAVY = "heavy"
TIER_LIGHT = "light"


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


def tier(cls: ResourceClass) -> str:
    return TIER_HEAVY if cls in HEAVY_CLASSES else TIER_LIGHT


def tier_limits(*, batch_size: int, heavy_batch_size: int) -> dict[str, int]:
    """In-flight caps per tier: light uses ``batch_size``, heavy ``heavy_batch_size``."""
    return {
        TIER_LIGHT: max(1, int(batch_size)),
        TIER_HEAVY: max(1, int(heavy_batch_size)),
    }


def can_admit(
    cls: ResourceClass,
    *,
    in_flight: Mapping[ResourceClass, int],
    limits: Mapping[str, int],
) -> bool:
    """Whether one more trial of ``cls`` may start given current in-flight counts."""
    running = {TIER_LIGHT: 0, TIER_HEAVY: 0}
    for running_cls, count in in_flight.items():
        running[tier(running_cls)] += max(0, int(count))
    own = tier(cls)
    other = TIER_HEAVY if own == TIER_LIGHT else TIER_LIGHT
    return running[other] == 0 and running[own] < limits[own]


def pick_admissible(
    pending: Sequence[Any],
    *,
    in_flight: Mapping[ResourceClass, int],
    limits: Mapping[str, int],
    classify: Callable[[Any], ResourceClass] = resource_class,
) -> int | None:
    """Return the index of the next pending trial to start, or ``None``.

    Scans in order and admits the first trial that ``can_admit``. Once the
    scan reaches a heavy trial that cannot start, it stops: admitting later
    light trials would keep the host busy and starve the heavy trial until
    every light trial had run. Light trials drain, then heavy trials start.
    While heavy trials run, the scan skips blocked light trials so later heavy
    trials can fill the heavy slots. ``classify`` maps a pending item to its
    class (``Trial`` by default; pass ``resource_class_for_row`` for case rows).
    """
    for index, trial in enumerate(pending):
        cls = classify(trial)
        if can_admit(cls, in_flight=in_flight, limits=limits):
            return index
        if tier(cls) == TIER_HEAVY:
            return None
    return None
