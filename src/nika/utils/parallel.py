"""Thread-pool helper that keeps the caller's context in worker threads."""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable, TypeVar

_T = TypeVar("_T")
_R = TypeVar("_R")


def bounded_parallel_map(
    function: Callable[[_T], _R], items: Iterable[_T], *, max_workers: int = 8
) -> list[_R]:
    """Map independent tasks concurrently and preserve input order.

    Each task runs in a copy of the caller's ``contextvars`` context, so
    per-trial log bindings (session dir, trial id) reach worker threads and
    their events land in the right ``nika.jsonl``.
    """
    values = list(items)
    if len(values) < 2 or max_workers < 2:
        return [function(item) for item in values]
    # One copy per task: a Context cannot be entered by two threads at once.
    contexts = [contextvars.copy_context() for _ in values]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(values))) as pool:
        return list(
            pool.map(lambda ctx, item: ctx.run(function, item), contexts, values)
        )
