"""Benchmark test helpers."""

from __future__ import annotations


def pytest_runtest_setup(item) -> None:
    # Temporary: identify which unit+contract test stalls on CI runners.
    print(f"\n>>> RUNNING {item.nodeid}", flush=True)
