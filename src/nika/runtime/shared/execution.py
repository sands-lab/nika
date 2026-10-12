"""Timed command execution shared by lab runtime backends."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from func_timeout import FunctionTimedOut, func_timeout


def without_shell_history(cmd: str) -> str:
    """Keep runtime commands out of node shell histories.

    vtysh records every ``-c`` command in ``~/.history_frr``; injection and
    verification commands left there would point at the faulty router.
    """
    return f"export VTYSH_HISTFILE=/dev/null; {cmd}"


_FAILURE_LOGS: list[list[dict[str, Any]]] = []
_FAILURE_LOCK = threading.Lock()


def note_exec_failure(node: str, cmd: str, error: str) -> None:
    """Record a command that timed out or raised for every open collector."""
    with _FAILURE_LOCK:
        for log in _FAILURE_LOGS:
            log.append({"node": node, "cmd": cmd[:300], "error": error[:300]})


@contextmanager
def collect_exec_failures() -> Iterator[list[dict[str, Any]]]:
    """Collect lab commands that timed out or raised while the block runs.

    Probes reduce command output to booleans, so a timed-out ``ping`` reads the
    same as an unreachable host. Callers that must tell them apart inspect the
    collected failures.
    """
    log: list[dict[str, Any]] = []
    with _FAILURE_LOCK:
        _FAILURE_LOGS.append(log)
    try:
        yield log
    finally:
        with _FAILURE_LOCK:
            _FAILURE_LOGS.remove(log)


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
        note_exec_failure(node, cmd, f"timeout after {timeout}s")
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
