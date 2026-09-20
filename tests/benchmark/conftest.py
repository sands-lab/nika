"""Benchmark test helpers."""

from __future__ import annotations

import sys


def pytest_runtest_setup(item) -> None:
    # Bypass pytest capture so CI logs show the in-flight test on hang.
    sys.__stderr__.write(f">>> RUNNING {item.nodeid}\n")
    sys.__stderr__.flush()
