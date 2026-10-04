"""Timed command execution shared by lab runtime backends."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from func_timeout import FunctionTimedOut, func_timeout


def without_shell_history(cmd: str) -> str:
    """Keep runtime commands out of node shell histories.

    vtysh records every ``-c`` command in ``~/.history_frr``; injection and
    verification commands left there would point at the faulty router.
    """
    return f"export VTYSH_HISTFILE=/dev/null; {cmd}"


def exec_with_timeout(
    run: Callable[[], str],
    *,
    timeout: float,
    node: str,
    cmd: str,
) -> str:
    try:
        return func_timeout(timeout, run)
    except FunctionTimedOut:
        return f"[TIMEOUT] Command '{cmd}' on '{node}' exceeded {timeout}s."


def _as_text(value: Any) -> str:
    if value is None or isinstance(value, int):
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def merge_exec_output(stdout: Any, stderr: Any) -> str:
    """Return one exec's output the same way on every backend.

    stdout and stderr are each stripped and joined with a newline; either may
    be empty. No exit-code markers are added, so callers parse identical text
    on Kathara and Containerlab.
    """
    parts = (_as_text(stdout).strip(), _as_text(stderr).strip())
    return "\n".join(part for part in parts if part)
