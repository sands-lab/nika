"""Sandbox runtime interface used by :mod:`agent.sandbox.manager`.

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

from agent.sandbox.config import SandboxConfig

DEFAULT_SANDBOX_BACKEND = "sbx"


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

    def __init__(self, config: SandboxConfig) -> None:
        self.config = config

    @abstractmethod
    def ensure_available(self) -> None:
        """Raise ``RuntimeError`` when this runtime cannot be used on the host."""

    @abstractmethod
    def prepare_host(
        self, *, agent_type: str, llm_provider: str | None
    ) -> dict[str, str]:
        """Prepare host-side access for one session.

        Returns the credential env for the sandbox. Values must be placeholders
        or per-session tokens, never real provider keys.
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
        """Copy workspace files from the host into the sandbox."""

    def pull_files(
        self, sandbox_id: str, workspace_dir: Path, names: Sequence[str]
    ) -> None:
        """Copy workspace files from the sandbox to the host."""

    @abstractmethod
    def remove(self, sandbox_id: str) -> None:
        """Delete the sandbox."""

    def start_log_fields(self) -> dict[str, Any]:
        """Extra ``sandbox_start`` event fields describing the runtime."""
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
        from agent.sandbox.sbx.backend import SbxBackend

        return SbxBackend
    raise ValueError(f"Unknown sandbox backend: {name!r}")


def get_backend(config: SandboxConfig) -> SandboxBackend:
    """Instantiate the backend selected by *config*."""
    return backend_class(config.backend)(config)


def cleanup_sandbox_for_session(
    session_meta: Mapping[str, Any] | None,
) -> SandboxCleanupResult | None:
    """Remove a session's sandbox with the backend that created it."""
    if not session_meta:
        return None
    name = str(session_meta.get("sandbox_backend") or DEFAULT_SANDBOX_BACKEND)
    return backend_class(name).cleanup_session(session_meta)
