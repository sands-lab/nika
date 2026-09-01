"""Timed command execution shared by lab runtime backends."""

from __future__ import annotations

from collections.abc import Callable

from func_timeout import FunctionTimedOut, func_timeout


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
