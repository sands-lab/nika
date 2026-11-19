"""Diagnosis objectives a task asks for: detection, localization, and RCA.

A task row may set ``objectives`` to a subset that always contains ``rca``.
Rows without the field ask for all three, and keep their published identity.
Scores of objectives a task does not ask for are ``null``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

DETECTION = "detection"
LOCALIZATION = "localization"
RCA = "rca"
OBJECTIVES: tuple[str, ...] = (DETECTION, LOCALIZATION, RCA)

# Score keys that belong to an objective other than RCA.
OBJECTIVE_SCORE_KEYS: dict[str, tuple[str, ...]] = {
    DETECTION: ("detection_score",),
    LOCALIZATION: (
        "localization_accuracy",
        "localization_precision",
        "localization_recall",
        "localization_f1",
    ),
}


def normalize_objectives(value: str | Iterable[str] | None) -> tuple[str, ...]:
    """Return the objectives in canonical order; ``None`` means all three."""
    if value is None:
        return OBJECTIVES
    chosen = {value} if isinstance(value, str) else set(value)
    unknown = sorted(chosen - set(OBJECTIVES))
    if unknown:
        raise ValueError(
            f"Unknown objectives {unknown}; supported: {list(OBJECTIVES)}."
        )
    if RCA not in chosen:
        raise ValueError("Task objectives must include 'rca'.")
    return tuple(name for name in OBJECTIVES if name in chosen)


def objectives_field(value: str | Iterable[str] | None) -> dict[str, list[str]]:
    """Return ``{"objectives": [...]}`` for a task row, or ``{}`` for all three."""
    objectives = normalize_objectives(value)
    return {} if objectives == OBJECTIVES else {"objectives": list(objectives)}


def objectives_prompt(value: str | Iterable[str] | None) -> str:
    """Task-description sentence for a task that does not ask for all three."""
    objectives = normalize_objectives(value)
    if objectives == OBJECTIVES:
        return ""
    names = {
        DETECTION: "whether there is an anomaly",
        LOCALIZATION: "which devices or links are faulty",
        RCA: "the root cause of each fault and where it is",
    }
    asked = "; ".join(names[name] for name in objectives)
    return f"\n\nThis task is scored only on: {asked}."


def drop_unasked_scores(scores: dict[str, Any], gt: dict[str, Any]) -> dict[str, Any]:
    """Set the scores of objectives the task does not ask for to ``None``."""
    objectives = normalize_objectives(gt.get("objectives"))
    for name, keys in OBJECTIVE_SCORE_KEYS.items():
        if name not in objectives:
            scores.update(dict.fromkeys(keys))
    return scores
