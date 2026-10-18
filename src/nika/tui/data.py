"""Catalog lookups for the TUI pickers, built on existing NIKA loaders."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from nika.config import REPO_ROOT

CONFIG_DIR = REPO_ROOT / "config"


@dataclass
class AgentProfile:
    """One run-config YAML that selects an agent, provider, and model."""

    path: Path
    agent_type: str
    provider: str
    model: str
    base_url: str | None
    template: bool

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def display_path(self) -> str:
        try:
            return str(self.path.relative_to(REPO_ROOT))
        except ValueError:
            return str(self.path)


def load_profile(path: Path) -> AgentProfile:
    from nika.run_config.loader import load_run_config

    cfg = load_run_config(path)
    return AgentProfile(
        path=path,
        agent_type=cfg.agent.type,
        provider=cfg.agent.provider,
        model=cfg.agent.model or "",
        base_url=cfg.agent.base_url,
        template=path.name.endswith(".example.yaml"),
    )


def list_profiles() -> list[AgentProfile]:
    """Run configs under ``config/`` (recursively): user copies first, then shipped templates."""
    profiles: list[AgentProfile] = []
    paths = sorted(
        CONFIG_DIR.rglob("*.yaml"),
        key=lambda p: (
            p.name.endswith(".example.yaml"),
            p != CONFIG_DIR / "nika.yaml",
            p.relative_to(CONFIG_DIR).as_posix(),
        ),
    )
    for path in paths:
        try:
            profiles.append(load_profile(path))
        except (OSError, ValueError, yaml.YAMLError):
            continue
    return profiles


@dataclass
class Task:
    """A task label accepted by ``nika agent run --problem``."""

    label: str
    scenario: str
    problem: str
    topo_size: str
    domain: str
    description: str
    presets: list[dict[str, Any]] = field(default_factory=list)


class TaskCatalog:
    """Distinct task labels from the example-case matrix, loaded once in the background."""

    def __init__(self) -> None:
        self._tasks: list[Task] | None = None
        self.error: str | None = None
        self.source = ""
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._load, name="nika-tui-tasks", daemon=True).start()

    @property
    def ready(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: float | None = None) -> list[Task]:
        self.start()
        self._done.wait(timeout)
        return self._tasks or []

    def _load(self) -> None:
        from nika.workflows.case import DEFAULT_CATALOG, load_example_cases

        try:
            try:
                rows = load_example_cases()
                self.source = "registered cases"
            except Exception as exc:  # generated cases need Docker for some labs
                rows = load_example_cases(DEFAULT_CATALOG)
                self.source = "working pool"
                self.error = f"Generated cases unavailable ({type(exc).__name__}); showing the working pool."
            self._tasks = _group_tasks(rows)
        except Exception as exc:
            self.error = f"Could not load tasks: {exc}"
            self._tasks = []
        finally:
            self._done.set()


def _group_tasks(rows: list[dict[str, Any]]) -> list[Task]:
    from nika.net_env.net_env_pool import scenario_requires_topo_size
    from nika.problems.registry import get_problem_class
    from nika.workflows.case import DEPLOY_KEYS
    from nika.workflows.benchmark.task_label import format_task_label

    tasks: dict[str, Task] = {}
    for row in rows:
        size = (
            row.get("topo_size")
            if scenario_requires_topo_size(row["scenario"])
            else None
        )
        # agent run --problem deploys scenario defaults; skip ISP option variants.
        if any(
            row.get(key) not in (None, "") for key in DEPLOY_KEYS if key != "topo_size"
        ):
            continue
        try:
            label = format_task_label(row["scenario"], row["problem"], size)
        except ValueError:
            continue
        task = tasks.get(label)
        if task is None:
            cls = get_problem_class(row["problem"], row["scenario"])
            domain = getattr(cls, "failure_domain", None) or ""
            task = tasks[label] = Task(
                label=label,
                scenario=row["scenario"],
                problem=row["problem"],
                topo_size=size or "",
                domain=str(getattr(domain, "value", domain)),
                description=" ".join(
                    str(getattr(cls, "description", "") or "").split()
                ),
            )
        task.presets.append(row)
    return sorted(tasks.values(), key=lambda t: t.label)


def running_sessions() -> list[dict[str, Any]]:
    from nika.workflows.session.list import list_sessions

    try:
        return list_sessions(running_only=True)
    except Exception:
        return []


def session_hosts(session_id: str) -> list[str]:
    from nika.workflows.session.containers import list_session_containers

    _, _, containers = list_session_containers(session_id)
    return sorted({str(c.get("name")) for c in containers if c.get("name")})


def releases() -> list[tuple[str, bool]]:
    """(version, deprecated) for each frozen release, newest first."""
    from nika.workflows.benchmark.release import is_deprecated_release, list_releases

    return [(v, is_deprecated_release(v)) for v in reversed(list_releases())]
