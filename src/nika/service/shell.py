"""Shared shell resolution and command wrapping for container exec."""

from __future__ import annotations

import uuid
from typing import Protocol


SHELL_PROBE_CMD = (
    "/bin/sh -c 'if [ -x /bin/bash ]; then echo /bin/bash; "
    "elif [ -x /bin/sh ]; then echo /bin/sh; else echo /bin/sh; fi'"
)


def escape_for_shell_c(command: str) -> str:
    # Only single quotes need escaping inside '...'; backslashes there are
    # literal, so escaping double quotes would split quoted arguments.
    return command.replace("'", "'\\''")


def wrap_shell_command(shell: str, command: str) -> str:
    escaped = escape_for_shell_c(command)
    return f"{shell} -c '{escaped}'"


def ping_exec_timeout(count: int) -> float:
    """Exec budget for ``ping -c count``: ~1 s per probe plus iputils' ~10 s
    linger when no reply arrives, so loss reads as loss, not a timeout."""
    return float(max(int(count), 1)) + 12.0


def iperf_server_commands(server_args: str = "") -> tuple[str, str]:
    """Return ``(start, stop)`` shell commands for a one-off iperf3 daemon.

    The daemon records its PID so ``stop`` kills only this server, never
    iperf3 processes that scenario traffic generators run in the background.
    """
    pidfile = f"/tmp/nika-iperf3-{uuid.uuid4().hex[:12]}.pid"
    start = f"iperf3 -s -D -I {pidfile} {server_args}".rstrip()
    stop = (
        f'[ -s {pidfile} ] && kill "$(cat {pidfile})" 2>/dev/null; '
        f"rm -f {pidfile}; true"
    )
    return start, stop


class ExecFn(Protocol):
    def __call__(self, node: str, cmd: str, *, timeout: float = 10.0) -> str: ...


class ShellResolver:
    """Cache per-node shell paths discovered via exec or lab metadata."""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}

    def resolve(
        self,
        node: str,
        exec_fn: ExecFn,
        *,
        preferred_shell: str | None = None,
    ) -> str:
        cached = self._cache.get(node)
        if cached is not None:
            return cached
        if preferred_shell is not None:
            self._cache[node] = preferred_shell
            return preferred_shell
        probed = exec_fn(node, SHELL_PROBE_CMD).strip()
        shell = probed if probed in ("/bin/bash", "/bin/sh") else "/bin/sh"
        self._cache[node] = shell
        return shell

    def exec_via_shell(
        self,
        node: str,
        command: str,
        exec_fn: ExecFn,
        *,
        preferred_shell: str | None = None,
        timeout: float = 10.0,
    ) -> str:
        shell = self.resolve(node, exec_fn, preferred_shell=preferred_shell)
        wrapped = wrap_shell_command(shell, command)
        return exec_fn(node, wrapped, timeout=timeout)
