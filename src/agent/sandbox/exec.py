"""Run commands in the session sandbox from host-side agent workers.

CLI agents keep their worker on the host and run only the agent CLI inside the
sandbox. :func:`exec_in_sandbox` starts a command through the backend that
created the active sandbox. :func:`push_to_sandbox` and
:func:`pull_from_sandbox` move the workspace files the command reads and
writes; they do nothing on runtimes that share the workspace (sbx).
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Iterable
from pathlib import Path

from agent.sandbox.backend import AsyncProcess, active_sandbox_backend
from agent.sandbox.config import (
    ENV_SANDBOX_NAME,
    ENV_SESSION_DIR,
    sandbox_name_from_env,
)
from agent.sandbox.constants import PROXY_TOKEN_PREFIX
from agent.sandbox.sbx.auth import PROXY_MANAGED_SENTINEL
from agent.utils.provider_env import CUSTOM_UNAUTHENTICATED_API_KEY

_INNER_ENV_ALLOWLIST = frozenset(
    {
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "DISABLE_AUTOUPDATER",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
        "CLAUDE_CODE_MCP_STARTUP_WAIT_MS",
        "MCP_TIMEOUT",
    }
)
_PATH_ENV_KEYS = frozenset({"CODEX_HOME", "CLAUDE_CONFIG_DIR"})
_SECRET_ENV_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
    }
)


def _sandbox_root() -> Path | None:
    raw = os.environ.get(ENV_SESSION_DIR, "").strip()
    if not raw:
        return None
    return Path(raw).resolve()


def _inner_path(
    path: str,
    *,
    sandbox_root: Path | None,
    cwd: Path | None,
) -> str:
    resolved = Path(path).resolve()
    if cwd is not None:
        try:
            return str(resolved.relative_to(cwd))
        except ValueError:
            pass
    if sandbox_root is not None:
        try:
            return str(resolved.relative_to(sandbox_root))
        except ValueError:
            pass
    return str(resolved)


def is_credential_placeholder(value: str) -> bool:
    """True for sandbox credential stand-ins, never a real provider key."""
    # A model proxy token only authorizes the host-side proxy of one session.
    return (
        value == PROXY_MANAGED_SENTINEL
        or value == CUSTOM_UNAUTHENTICATED_API_KEY
        or value.startswith("sbx-cs-")
        or value.startswith(PROXY_TOKEN_PREFIX)
    )


def _sandbox_env_value(key: str, value: str, *, full_env: dict[str, str]) -> str:
    """Never forward real credentials into the sandbox."""
    if key == "CLAUDE_CODE_OAUTH_TOKEN":
        return value if value.startswith(PROXY_TOKEN_PREFIX) else ""
    if key not in _SECRET_ENV_KEYS:
        return value
    if is_credential_placeholder(value):
        return value
    # Third-party Anthropic-compatible APIs: omit real host keys.
    if key.startswith("ANTHROPIC"):
        base = full_env.get("ANTHROPIC_BASE_URL", "").strip()
        if base:
            from agent.sandbox.sbx.credentials import is_official_anthropic_base_url

            if not is_official_anthropic_base_url(base):
                return ""
    return PROXY_MANAGED_SENTINEL


def sandbox_shell_command(
    command: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Return the ``bash -lc`` script that runs *command* in the sandbox.

    The script starts in the workspace root, so *cwd* and path variables are
    given relative to it. Only allowlisted variables are exported, with real
    credentials replaced by placeholders.
    """
    sandbox_root = _sandbox_root()
    cwd_path = Path(cwd).resolve() if cwd else None
    inner_cwd = _inner_path(cwd, sandbox_root=sandbox_root, cwd=None) if cwd else None

    exports: list[str] = []
    if env:
        for key, value in env.items():
            if key not in _INNER_ENV_ALLOWLIST:
                continue
            text = str(value).strip()
            if not text:
                continue
            if key in _PATH_ENV_KEYS:
                text = _inner_path(text, sandbox_root=sandbox_root, cwd=cwd_path)
            else:
                text = _sandbox_env_value(key, text, full_env=env)
                if not text:
                    continue
            exports.append(f"{key}={shlex.quote(text)}")

    inner_cmd = " ".join(shlex.quote(part) for part in command)
    if exports:
        inner_cmd = " ".join(exports + [inner_cmd])
    if inner_cwd:
        inner_cmd = f"cd {shlex.quote(inner_cwd)} && {inner_cmd}"
    return inner_cmd


def _active_sandbox(sandbox_name: str | None = None) -> str:
    name = sandbox_name or sandbox_name_from_env()
    if not name:
        raise RuntimeError(f"{ENV_SANDBOX_NAME} is not set for sandbox execution")
    return name


async def exec_in_sandbox(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    sandbox_name: str | None = None,
) -> AsyncProcess:
    """Start *command* inside the active sandbox and return the process handle."""
    name = _active_sandbox(sandbox_name)
    shell_command = sandbox_shell_command(command, cwd=cwd, env=env)
    return await active_sandbox_backend(name).exec_async(name, shell_command)


def _workspace_names(paths: Iterable[Path]) -> tuple[Path, list[str]]:
    root = _sandbox_root()
    if root is None:
        raise RuntimeError(f"{ENV_SESSION_DIR} is not set for sandbox execution")
    return root, [str(Path(path).resolve().relative_to(root)) for path in paths]


def push_to_sandbox(paths: Iterable[Path]) -> None:
    """Copy workspace files or directories from the host into the sandbox."""
    name = _active_sandbox()
    root, names = _workspace_names(paths)
    active_sandbox_backend(name).push_files(name, root, names)


def pull_from_sandbox(paths: Iterable[Path]) -> None:
    """Copy workspace files the sandbox wrote back to the host."""
    name = _active_sandbox()
    root, names = _workspace_names(paths)
    active_sandbox_backend(name).pull_files(name, root, names)
