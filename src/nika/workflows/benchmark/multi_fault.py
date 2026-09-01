"""Helpers for multi-fault benchmark rows and injection overrides."""

from __future__ import annotations

from typing import Any


def join_problem_label(problems: list[str]) -> str:
    return "+".join(problems)


def row_problems(row: dict[str, Any]) -> list[str]:
    raw = row.get("problems")
    if isinstance(raw, list) and raw:
        return [str(item) for item in raw]
    problem = str(row.get("problem") or "")
    if "+" in problem:
        return [part for part in problem.split("+") if part]
    if problem:
        return [problem]
    return []


def is_multi_fault_row(row: dict[str, Any]) -> bool:
    return len(row_problems(row)) > 1


def nested_inject_map(row: dict[str, Any]) -> dict[str, dict[str, str]]:
    problems = row_problems(row)
    inject = dict(row.get("inject") or {})
    if not problems:
        return {}
    if len(problems) == 1:
        return {problems[0]: {str(k): str(v) for k, v in inject.items()}}
    nested: dict[str, dict[str, str]] = {}
    for problem in problems:
        piece = inject.get(problem)
        if not isinstance(piece, dict) or not piece:
            raise ValueError(
                f"Multi-fault case must provide inject.{problem} mapping; got {piece!r}."
            )
        nested[problem] = {str(k): str(v) for k, v in piece.items()}
    return nested


def flatten_inject_overrides(row: dict[str, Any]) -> dict[str, Any]:
    """Return inject overrides suitable for ``inject_failure``."""
    return nested_inject_map(row)
