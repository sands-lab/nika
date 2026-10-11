"""Agents developed outside the NIKA source tree.

NIKA resolves an external agent in three ways:

* ``nika agent add <dir>`` records the directory of a project that ships a
  ``nika-agent.yaml`` manifest in the user registry (``config/agents.yaml``).
* An installed package exposes the ``nika.agents`` entry point group.
* ``-a module:Class`` names the agent class directly, with default settings.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.registry import ALL_PROVIDERS, BUILTIN_AGENTS, AgentSpec

MANIFEST_FILENAME = "nika-agent.yaml"
ENTRY_POINT_GROUP = "nika.agents"
ENV_AGENTS_FILE = "NIKA_AGENTS_FILE"
DEFAULT_AGENTS_FILE_REL = Path("config") / "agents.yaml"

_FACTORY_RE = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_BUILTIN_IDS = frozenset(spec.id for spec in BUILTIN_AGENTS)


class SandboxDefinition(BaseModel):
    """How to install an external agent into an isolated sandbox.

    NIKA keeps the network policy, credentials, and phase boundary; the agent
    only declares what to install and which LLM hosts it must reach.
    """

    model_config = ConfigDict(extra="forbid")

    base: str = Field(description="Base image, e.g. python:3.12-slim.")
    install: list[str] = Field(default_factory=list)
    egress: list[str] = Field(default_factory=list)
    # Defaults to NIKA's in-sandbox runner, which imports ``factory``.
    entrypoint: str | None = None


class AgentManifest(BaseModel):
    """Contents of ``nika-agent.yaml``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    factory: str
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
        return value

    @field_validator("factory")
    @classmethod
    def _check_factory(cls, value: str) -> str:
        if not _FACTORY_RE.match(value):
            raise ValueError("expected 'module.path:ClassName'")
        return value

    def to_spec(self, *, source: str) -> AgentSpec:
        return AgentSpec(
            id=self.id,
            factory=self.factory,
            providers=frozenset(self.providers),
            credential_family=self.credential_family,
            supports_max_tokens=self.supports_max_tokens,
            default_model=self.default_model,
            source=source,
            sandbox_definition=(
                self.sandbox.model_dump() if self.sandbox is not None else None
            ),
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
    """Return ``{agent_id: {"path": ..., "package": ...}}`` from the user registry."""
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


def _registered_spec(agent_id: str, entry: dict[str, Any]) -> AgentSpec:
    try:
        manifest = load_manifest(entry["path"])
    except FileNotFoundError as exc:
        raise ValueError(
            f"Agent {agent_id!r} is registered but {exc}; run `nika agent add` "
            "with its new location or `nika agent remove`."
        ) from exc
    if manifest.id != agent_id:
        raise ValueError(
            f"{entry['path']}/{MANIFEST_FILENAME} now declares id {manifest.id!r}, "
            f"not {agent_id!r}; run `nika agent add` again."
        )
    return manifest.to_spec(source="user")


def _entry_point_spec(ep: Any) -> AgentSpec:
    target = ep.load()
    if isinstance(target, AgentSpec):
        return replace(target, source="entry point")
    return AgentSpec(id=ep.name, factory=ep.value, source="entry point")


def find_external_agent_spec(agent_type: str) -> AgentSpec | None:
    """Resolve a non-built-in agent, or return ``None`` when it is unknown."""
    if entry := read_registered_agents().get(agent_type):
        return _registered_spec(agent_type, entry)
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if ep.name == agent_type:
            return _entry_point_spec(ep)
    if _FACTORY_RE.match(agent_type):
        return AgentSpec(id=agent_type, factory=agent_type, source="import path")
    return None


def list_external_agents() -> list[tuple[str, AgentSpec | None, str]]:
    """Return ``(agent_id, spec or None, problem)`` for every external agent."""
    found: list[tuple[str, AgentSpec | None, str]] = []
    for agent_id, entry in read_registered_agents().items():
        try:
            found.append((agent_id, _registered_spec(agent_id, entry), ""))
        except ValueError as exc:
            found.append((agent_id, None, str(exc)))
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            found.append((ep.name, _entry_point_spec(ep), ""))
        except Exception as exc:  # a broken plugin must not hide the others
            found.append((ep.name, None, str(exc)))
    return found
