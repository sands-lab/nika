"""Agents developed outside the NIKA source tree.

NIKA resolves an external agent in two ways:

* ``nika agent add <dir>`` records the directory of a project that ships a
  ``nika-agent.yaml`` manifest in the user registry (``config/agents.yaml``).
* ``-a module:Class`` names an agent class that NIKA's interpreter can find
  (e.g. through ``PYTHONPATH``), with default settings.

Either way the agent runs in its own sandbox, like the built-in agents: NIKA
copies the project into the sandbox workspace and installs its dependencies
there, never into NIKA's environment.
"""

from __future__ import annotations

import importlib.util
import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from nika.agents.registry import ALL_PROVIDERS, BUILTIN_AGENTS, AgentSpec

MANIFEST_FILENAME = "nika-agent.yaml"
ENV_AGENTS_FILE = "NIKA_AGENTS_FILE"
DEFAULT_AGENTS_FILE_REL = Path("config") / "agents.yaml"

_CLASS_RE = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_BUILTIN_IDS = frozenset(spec.id for spec in BUILTIN_AGENTS)
# Requirement set every external agent gets (see nika.agents.sandbox.wheels).
EXTERNAL_AGENT_REQUIREMENTS = "langgraph"


class SandboxDefinition(BaseModel):
    """How to install an external agent into its sandbox.

    NIKA keeps the network policy, credentials, and phase boundary; the agent
    only declares its sandbox template, what to install, and which extra hosts
    (e.g. its LLM endpoint) it must reach.
    """

    model_config = ConfigDict(extra="forbid")

    # Before NIKA ran added agents in sandboxes, this definition was validated
    # but never executed, and its fields meant something else: ``base`` named
    # a container image, ``install`` held shell commands, and ``entrypoint``
    # replaced the runner. Sandboxes now start from NIKA's pinned templates
    # (Docker Sandboxes and E2B both run them), the project is copied into the
    # workspace, and every agent runs through ``agent.runtime``.
    base: Literal["shell", "claude", "codex"] = Field(
        default="shell",
        description="Sandbox template: shell, or one with the Claude/Codex CLI.",
    )
    # pip requirement specifiers, installed from PyPI after NIKA's pinned
    # ``langgraph`` set; default: the project's [project].dependencies.
    install: list[str] | None = None
    # Hosts the sandbox may reach while it is prepared, before the agent starts.
    egress: list[str] = Field(default_factory=list)


class AgentManifest(BaseModel):
    """Contents of ``nika-agent.yaml``."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    class_path: str = Field(alias="class")
    providers: list[Literal["openai", "anthropic", "deepseek", "custom"]] = Field(
        default_factory=lambda: sorted(ALL_PROVIDERS)
    )
    credential_family: Literal["openai", "anthropic"] = "openai"
    supports_max_tokens: bool = True
    default_model: str | None = None
    sandbox: SandboxDefinition | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _AGENT_ID_RE.match(value):
            raise ValueError(
                "use lowercase letters, digits, '.', '_' or '-' (e.g. 'nooa')"
            )
        if value in _BUILTIN_IDS:
            raise ValueError(f"{value!r} is a built-in NIKA agent")
        if value.startswith("custom."):
            raise ValueError("ids starting with 'custom.' name src/agent/custom files")
        return value

    @field_validator("class_path")
    @classmethod
    def _check_class_path(cls, value: str) -> str:
        if not _CLASS_RE.match(value):
            raise ValueError("expected 'module.path:ClassName'")
        return value

    def to_spec(self, *, source: str, import_root: str) -> AgentSpec:
        sandbox = self.sandbox or SandboxDefinition()
        packages = (
            sandbox.install
            if sandbox.install is not None
            else project_dependencies(Path(import_root))
        )
        return AgentSpec(
            id=self.id,
            class_path=self.class_path,
            import_root=import_root,
            providers=frozenset(self.providers),
            credential_family=self.credential_family,
            supports_max_tokens=self.supports_max_tokens,
            default_model=self.default_model,
            template=sandbox.base,
            # The default structured submission runs the LangGraph worker.
            requirements=EXTERNAL_AGENT_REQUIREMENTS,
            packages=tuple(packages),
            requires_python=project_requires_python(Path(import_root)),
            egress=tuple(sandbox.egress),
            source=source,
        )


def load_manifest(path: str | Path) -> AgentManifest:
    """Read ``nika-agent.yaml`` from a project directory or a manifest path."""
    path = Path(path).expanduser()
    manifest_path = path / MANIFEST_FILENAME if path.is_dir() else path
    if not manifest_path.is_file():
        raise FileNotFoundError(f"No {MANIFEST_FILENAME} found at {path}")
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    return AgentManifest.model_validate(data)


def agents_file() -> Path:
    """User registry written by ``nika agent add`` (gitignored under config/)."""
    if override := os.environ.get(ENV_AGENTS_FILE, "").strip():
        return Path(override).expanduser().resolve()
    from nika.config import REPO_ROOT

    return (REPO_ROOT / DEFAULT_AGENTS_FILE_REL).resolve()


def read_registered_agents() -> dict[str, dict[str, Any]]:
    """Return ``{agent_id: {"path": ...}}`` from the user registry."""
    path = agents_file()
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return dict(data.get("agents") or {})


def write_registered_agents(agents: dict[str, dict[str, Any]]) -> None:
    path = agents_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump({"agents": dict(sorted(agents.items()))}, sort_keys=False)
    path.write_text(
        "# Written by `nika agent add`; edit with `nika agent add|remove`.\n" + text,
        encoding="utf-8",
    )


def import_root(project: Path, class_path: str) -> Path:
    """Directory to put on ``sys.path`` so *class_path* imports from *project*.

    Supports the flat layout (``<project>/<pkg>``) and the src layout
    (``<project>/src/<pkg>``).
    """
    top = class_path.partition(":")[0].split(".")[0]
    for root in (project, project / "src"):
        if (root / top).is_dir() or (root / f"{top}.py").is_file():
            return root
    raise ValueError(
        f"Cannot find module {top!r} in {project} or {project / 'src'}; "
        f"check `class` in {MANIFEST_FILENAME}."
    )


def check_import_root(root: Path) -> None:
    """Refuse an import root whose copy would hand NIKA's tree to the agent.

    The sandbox receives a copy of the import root, so a root that holds the
    NIKA checkout would expose its sources, ``.env``, and other sessions'
    results.
    """
    from nika.config import REPO_ROOT

    root = root.resolve()
    if root == REPO_ROOT or REPO_ROOT.is_relative_to(root) or (root / "nika").is_dir():
        raise ValueError(
            f"Agent code at {root} shares a directory with NIKA; move the agent "
            "into its own project and add it with `nika agent add`."
        )


def _project_table(root: Path) -> dict[str, Any]:
    """``[project]`` of the ``pyproject.toml`` at *root* or its parent.

    *root* is the import root, which is the project directory or its ``src``.
    """
    for project in (root, root.parent):
        pyproject = project / "pyproject.toml"
        if pyproject.is_file():
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            return data.get("project", {})
    return {}


def project_dependencies(root: Path) -> list[str]:
    """``[project].dependencies`` of the project at *root* or its parent."""
    return list(_project_table(root).get("dependencies", []))


def project_requires_python(root: Path) -> str | None:
    """``[project].requires-python`` of the project at *root* or its parent."""
    from nika.agents.sandbox.wheels import check_requires_python

    return check_requires_python(
        _project_table(root).get("requires-python"), where=str(root)
    )


def class_defined(root: Path, class_path: str) -> bool:
    """Whether the module of *class_path* under *root* defines its class.

    Checked without importing it: the agent's dependencies live in its sandbox,
    not in NIKA's environment.
    """
    module_name, _, attr = class_path.partition(":")
    base = root.joinpath(*module_name.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            source = path.read_text(encoding="utf-8")
            return (
                re.search(
                    rf"^(class\s+{re.escape(attr)}\b|{re.escape(attr)}\s*=)",
                    source,
                    re.MULTILINE,
                )
                is not None
            )
    return False


def _registered_spec(agent_id: str, entry: dict[str, Any]) -> AgentSpec:
    project = Path(entry["path"])
    try:
        manifest = load_manifest(project)
        root = import_root(project, manifest.class_path)
        check_import_root(root)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(
            f"Agent {agent_id!r} is registered but {exc}; run `nika agent add` "
            "with its new location or `nika agent remove`."
        ) from exc
    if manifest.id != agent_id:
        raise ValueError(
            f"{project}/{MANIFEST_FILENAME} now declares id {manifest.id!r}, "
            f"not {agent_id!r}; run `nika agent add` again."
        )
    return manifest.to_spec(source="user", import_root=str(root))


def _import_path_spec(class_path: str) -> AgentSpec | None:
    """Spec of an agent named by import path, found on NIKA's ``sys.path``."""
    top = class_path.partition(":")[0].split(".")[0]
    found = importlib.util.find_spec(top)
    origin = (found.origin or "") if found is not None else ""
    if not origin or origin in ("built-in", "frozen"):
        return None
    path = Path(origin)
    root = path.parent.parent if path.name == "__init__.py" else path.parent
    check_import_root(root)
    return AgentSpec(
        id=class_path,
        class_path=class_path,
        import_root=str(root),
        requirements=EXTERNAL_AGENT_REQUIREMENTS,
        packages=tuple(project_dependencies(root)),
        requires_python=project_requires_python(root),
        source="import path",
    )


def find_external_agent_spec(agent_type: str) -> AgentSpec | None:
    """Resolve a non-built-in agent, or return ``None`` when it is unknown."""
    if entry := read_registered_agents().get(agent_type):
        return _registered_spec(agent_type, entry)
    if _CLASS_RE.match(agent_type):
        return _import_path_spec(agent_type)
    return None


def list_external_agents() -> list[tuple[str, AgentSpec | None, str]]:
    """Return ``(agent_id, spec or None, problem)`` for every added agent."""
    found: list[tuple[str, AgentSpec | None, str]] = []
    for agent_id, entry in read_registered_agents().items():
        try:
            found.append((agent_id, _registered_spec(agent_id, entry), ""))
        except ValueError as exc:
            found.append((agent_id, None, str(exc)))
    return found
