"""Test-path failure symptom evaluation.

Production inject uses only artifact ``verify_fault``. Symptom checks live here
as ``evaluate_symptom`` (unified probe API, plus per-failure custom handlers).
"""

from tests.support.symptom.contracts import (
    SymptomContract,
    get_probe_path,
    get_symptom_contract,
    list_symptom_contracts,
)
from tests.support.symptom.evaluate import evaluate_symptom

__all__ = [
    "SymptomContract",
    "evaluate_symptom",
    "get_probe_path",
    "get_symptom_contract",
    "list_symptom_contracts",
]
