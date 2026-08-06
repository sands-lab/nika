"""Run commands inside a native Docker Sandbox via ``sbx exec``."""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

from agent.sandbox.config import ENV_SESSION_DIR
from agent.sandbox.sbx.agents import ENV_SBX_SANDBOX_NAME
from agent.sandbox.sbx.auth import PROXY_MANAGED_SENTINEL

_INNER_ENV_ALLOWLIST = frozenset(
    {
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "NIKA_LLM_PROVIDER",
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


def sandbox_name_from_env() -> str | None:
    name = os.environ.get(ENV_SBX_SANDBOX_NAME, "").strip()
    return name or None


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


def _looks_like_placeholder(value: str) -> bool:
    return value == PROXY_MANAGED_SENTINEL or value.startswith("sbx-cs-")


def _sandbox_env_value(key: str, value: str, *, full_env: dict[str, str]) -> str:
    """Never forward real credentials into the microVM for proxy-managed services."""
    if key not in _SECRET_ENV_KEYS:
        return value
    if _looks_like_placeholder(value):
        return value
    # Third-party Anthropic-compatible APIs: omit real host keys.
    if key.startswith("ANTHROPIC"):
        base = full_env.get("ANTHROPIC_BASE_URL", "").strip()
        if base:
            from agent.sandbox.sbx.credentials import is_official_anthropic_base_url

            if not is_official_anthropic_base_url(base):
                return ""
    return PROXY_MANAGED_SENTINEL


def build_sbx_exec_command(
    sandbox_name: str,
    command: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Build ``sbx exec -d`` argv for *command*."""
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
    return ["sbx", "exec", "-d", sandbox_name, "bash", "-lc", inner_cmd]


async def exec_in_sandbox(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
    sandbox_name: str | None = None,
) -> asyncio.subprocess.Process:
    """Start *command* inside the active sandbox and return the process handle."""
    name = sandbox_name or sandbox_name_from_env()
    if not name:
        raise RuntimeError(f"{ENV_SBX_SANDBOX_NAME} is not set for sandbox execution")

    argv = build_sbx_exec_command(name, command, cwd=cwd, env=env)
    return await asyncio.create_subprocess_exec(
        *argv,
        env=os.environ.copy(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=8 * 1024 * 1024,
    )
