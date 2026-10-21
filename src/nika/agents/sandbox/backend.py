"""Sandbox runtime interface used by :mod:`nika.agents.sandbox.manager`.

The manager owns everything that is the same for every runtime: the manifest,
the host workspace, the two-step SDK run, the trace mirror, and teardown order.
A backend owns what differs: host preparation and credentials, creating and
removing the sandbox, gateway reachability, command execution, and moving
workspace files between the host and the sandbox.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Protocol

from nika.agents.sandbox.config import SandboxConfig
from nika.agents.sandbox.model_calls import ModelCallLog


class StreamedProcess(Protocol):
    """Command running in a sandbox, with stdout and stderr merged."""

    stdout: Iterable[str] | None

    def wait(self) -> int: ...

    def kill(self) -> None: ...


@dataclass(frozen=True)
class SandboxCleanupResult:
    sandbox_name: str
    removed_sandbox: bool
    removed_workspace: bool

    @property
    def did_work(self) -> bool:
        return self.removed_sandbox or self.removed_workspace


class SandboxBackend(ABC):
    """One sandbox runtime.

    The manager prepares a workspace directory on the host for each session.
    Backends make it available inside the sandbox; a runtime that bind-mounts
    it (such as sbx) needs no work in :meth:`push_files` / :meth:`pull_files`.
    """

    name: ClassVar[str]
    # How sandboxes reach the session's MCP gateway: the host they address,
    # and the host interface the gateway binds (None: all interfaces).
    gateway_agent_host: ClassVar[str]
    gateway_bind_host: ClassVar[str | None] = None

    def __init__(self, config: SandboxConfig) -> None:
        self.config = config

    @abstractmethod
    def ensure_available(self) -> None:
        """Raise ``RuntimeError`` when this runtime cannot be used on the host."""

    @abstractmethod
    def prepare_templates(self, agent_types: Sequence[str]) -> list[str]:
        """Make the sandbox templates of *agent_types* ready (idempotent).

        Called when a job, environment, or setup starts, so the first session
        does not pay for image pulls or template builds. Returns the images the
        host Docker must hold; the caller pulls them with the lab images.
        """

    @abstractmethod
    def prepare_host(
        self,
        *,
        agent_type: str,
        llm_provider: str | None,
        call_log: ModelCallLog | None = None,
    ) -> dict[str, str]:
        """Prepare host-side access for one session.

        Every model request of the sandbox goes through the host model proxy,
        which holds the real credential and records the calls in *call_log*.
        Returns the model client env for the sandbox: the proxy URL and a
        per-session token, never a real provider key.
        """

    def workspace_path(self, workspace_dir: Path) -> str:
        """Return the path of the host workspace inside the sandbox."""
        return str(workspace_dir)

    def stage_workspace(self, workspace_dir: Path, *, agent_type: str) -> None:
        """Add runtime-specific files to the workspace before :meth:`create`."""

    @abstractmethod
    def create(
        self, *, agent_session_id: str, agent_type: str, workspace_dir: Path
    ) -> str:
        """Create a ready-to-use sandbox and return its id.

        On return, the content of *workspace_dir* is available inside the
        sandbox at :meth:`workspace_path`. Removes whatever was created when
        provisioning fails, so callers only clean up sandboxes whose id they
        received.
        """

    def sandbox_gateway_url(self, *, port: int, gateway_url: str) -> str:
        """Return the URL the sandbox uses for the gateway at *gateway_url*.

        *gateway_url* is where the gateway listens for sandboxes: on this host,
        or on a remote lab host. Called after :meth:`prepare_host`.
        """
        return gateway_url

    @abstractmethod
    def allow_gateway(self, sandbox_id: str, *, port: int, gateway_url: str) -> None:
        """Let the sandbox reach the session's MCP gateway."""

    @abstractmethod
    def revoke_gateway(self, sandbox_id: str, *, port: int, gateway_url: str) -> None:
        """Remove the access granted by :meth:`allow_gateway` (best effort)."""

    @abstractmethod
    def exec_stream(self, sandbox_id: str, command: str) -> StreamedProcess:
        """Start ``bash -lc command`` inside the sandbox."""

    def push_files(
        self, sandbox_id: str, workspace_dir: Path, names: Sequence[str]
    ) -> None:
        """Copy workspace files or directories from the host into the sandbox."""

    def pull_files(
        self, sandbox_id: str, workspace_dir: Path, names: Sequence[str]
    ) -> None:
        """Copy workspace files from the sandbox to the host."""

    @abstractmethod
    def remove(self, sandbox_id: str) -> None:
        """Delete the sandbox."""

    def release_host(self) -> None:
        """Stop host-side helpers started by :meth:`prepare_host`.

        Always called once the session ends, also when setup failed.
        """

    def start_log_fields(self) -> dict[str, Any]:
        """Extra ``sandbox_start`` event fields describing the runtime."""
        return {}

    @classmethod
    def session_fields(cls, config: SandboxConfig) -> dict[str, str]:
        """Session record fields :meth:`cleanup_session` needs under *config*."""
        return {}

    @classmethod
    @abstractmethod
    def cleanup_session(
        cls, session_meta: Mapping[str, Any]
    ) -> SandboxCleanupResult | None:
        """Remove the sandbox owned by *session_meta*, if any (best effort).

        Used by ``session close`` when the agent worker was hard-killed before
        the manager's own teardown ran. Must only touch this session's sandbox.
        """


def backend_class(name: str) -> type[SandboxBackend]:
    """Return the backend registered as *name*."""
    if name == "sbx":
        from nika.agents.sandbox.sbx.backend import SbxBackend

        return SbxBackend
    if name == "e2b":
        from nika.agents.sandbox.e2b.backend import E2bBackend

        return E2bBackend
    raise ValueError(f"Unknown sandbox backend: {name!r}")


def get_backend(config: SandboxConfig) -> SandboxBackend:
    """Instantiate the backend selected by *config*."""
    return backend_class(config.backend)(config)


def prepare_sandbox_templates(agent_types: Iterable[str]) -> list[str]:
    """Prepare the configured backend's templates for the sandboxed *agent_types*.

    Returns the images the host Docker must hold (see ``prepare_templates``).
    """
    from nika.agents.sandbox.config import resolve_sandbox_config
    from nika.agents.sandbox.templates import agent_template

    sandboxed = [agent for agent in dict.fromkeys(agent_types) if agent_template(agent)]
    if not sandboxed:
        return []
    return get_backend(resolve_sandbox_config()).prepare_templates(sandboxed)


def prepare_configured_sandbox_templates() -> list[str]:
    """Prepare sandbox templates for the agent selected in the run config."""
    from nika.run_config.loader import get_run_config

    return prepare_sandbox_templates([get_run_config().agent.type])


def cleanup_sandbox_for_session(
    session_meta: Mapping[str, Any] | None,
) -> SandboxCleanupResult | None:
    """Remove a session's sandbox with the backend that created it.

    Sessions record ``sandbox_backend`` before a sandboxed agent creates its
    sandbox, so a session without it has none to remove.
    """
    name = str((session_meta or {}).get("sandbox_backend") or "").strip()
    if not name:
        return None
    return backend_class(name).cleanup_session(session_meta)
