"""Per-failure symptom contracts read from the fault classes.

Each ``ProblemBase`` subclass declares ``symptom_probe`` and ``symptom_class``.
Benchmark inject uses only the artifact check ``verify_fault``; the audit and the
failure E2E tests measure the declared symptom. Scenario probe paths are declared
by each scenario (``nika.net_env.net_env_pool.get_probe_path``).
"""

from __future__ import annotations

from dataclasses import dataclass

from nika.net_env.net_env_pool import get_probe_path
from nika.validation.symptom.types import ProbeKind, SymptomClass

# Re-export for tests that need scenario paths via the same package.
__all__ = [
    "SymptomContract",
    "get_symptom_contract",
    "list_symptom_contracts",
    "get_probe_path",
]


@dataclass(frozen=True)
class SymptomContract:
    failure: str
    symptom_class: SymptomClass
    probe: ProbeKind
    loss_min_percent: float = 10.0
    latency_factor: float = 2.0


def _contract(cls: type) -> SymptomContract | None:
    if not cls.symptom_probe or not cls.symptom_class:
        return None
    return SymptomContract(
        failure=cls.root_cause_name,
        symptom_class=cls.symptom_class,
        probe=cls.symptom_probe,
    )


def get_symptom_contract(failure: str) -> SymptomContract | None:
    """The symptom the fault class declares, or ``None`` when it declares none."""
    from nika.problems.registry import get_problem_class

    cls = get_problem_class(failure)
    return None if cls is None else _contract(cls)


def list_symptom_contracts() -> list[SymptomContract]:
    from nika.problems.registry import list_avail_problem_instances

    contracts = (_contract(cls) for cls in list_avail_problem_instances().values())
    return sorted(
        (item for item in contracts if item is not None), key=lambda c: c.failure
    )
