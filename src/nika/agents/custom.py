"""Agents defined in one file under ``src/agent/custom``.

``src/agent/custom/<name>.py`` holding a ``DiagnosisAgent`` subclass is agent
type ``custom.<name>``. It runs like the built-in agents: the file ships with
the ``agent`` package into the sandbox, and NIKA installs what the class
declares:

* ``dependencies``: extra pip requirements, installed from PyPI;
* ``requires_python``: a version specifier for the agent's interpreter;
* ``providers``: LLM providers the agent supports (default: all).

NIKA reads these class attributes from the source, without importing the
module: the agent's dependencies exist only in its sandbox.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from nika.agents.plugins import EXTERNAL_AGENT_REQUIREMENTS
from nika.agents.registry import ALL_PROVIDERS, AgentSpec
from nika.agents.sandbox.wheels import check_requires_python

PREFIX = "custom."
_BASE_CLASS = "DiagnosisAgent"
_FIELDS = ("dependencies", "requires_python", "providers")


def custom_dir() -> Path:
    import agent

    return Path(agent.__file__).resolve().parent / "custom"


def _agent_class(tree: ast.Module, path: Path) -> ast.ClassDef:
    found = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(
            (isinstance(base, ast.Name) and base.id == _BASE_CLASS)
            or (isinstance(base, ast.Attribute) and base.attr == _BASE_CLASS)
            for base in node.bases
        )
    ]
    if len(found) != 1:
        raise ValueError(
            f"{path} must define exactly one {_BASE_CLASS} subclass; "
            f"found {len(found)}."
        )
    return found[0]


def _class_fields(cls: ast.ClassDef, path: Path) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for node in cls.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id in _FIELDS:
            try:
                fields[target.id] = ast.literal_eval(value)
            except ValueError as exc:
                raise ValueError(
                    f"{path}: {cls.name}.{target.id} must be a literal."
                ) from exc
    return fields


def _spec(path: Path) -> AgentSpec:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    cls = _agent_class(tree, path)
    fields = _class_fields(cls, path)
    dependencies = fields.get("dependencies", [])
    if not isinstance(dependencies, list | tuple) or not all(
        isinstance(item, str) for item in dependencies
    ):
        raise ValueError(f"{path}: {cls.name}.dependencies must be a list of strings.")
    requires_python = check_requires_python(
        fields.get("requires_python"), where=f"{path}: {cls.name}"
    )
    providers = frozenset(fields.get("providers") or ALL_PROVIDERS)
    if unknown := providers - ALL_PROVIDERS:
        raise ValueError(
            f"{path}: unknown providers {sorted(unknown)}; "
            f"choose from {sorted(ALL_PROVIDERS)}."
        )
    return AgentSpec(
        id=PREFIX + path.stem,
        class_path=f"agent.custom.{path.stem}:{cls.name}",
        providers=providers,
        requirements=EXTERNAL_AGENT_REQUIREMENTS,
        packages=tuple(dependencies),
        requires_python=requires_python,
        source="custom",
    )


def _agent_files() -> list[Path]:
    root = custom_dir()
    if not root.is_dir():
        return []
    return sorted(
        path
        for path in root.glob("*.py")
        if path.stem.isidentifier() and not path.stem.startswith("_")
    )


def find_custom_agent_spec(agent_type: str) -> AgentSpec | None:
    """Spec of ``custom.<name>``, or ``None`` when no such file exists."""
    name = agent_type.removeprefix(PREFIX)
    if name == agent_type or not name.isidentifier() or name.startswith("_"):
        return None
    path = custom_dir() / f"{name}.py"
    if not path.is_file():
        return None
    return _spec(path)


def list_custom_agents() -> list[tuple[str, AgentSpec | None, str]]:
    """Return ``(agent_id, spec or None, problem)`` for every custom agent file."""
    rows: list[tuple[str, AgentSpec | None, str]] = []
    for path in _agent_files():
        try:
            rows.append((PREFIX + path.stem, _spec(path), ""))
        except ValueError as exc:
            rows.append((PREFIX + path.stem, None, str(exc)))
    return rows
