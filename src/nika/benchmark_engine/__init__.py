"""Benchmark engines: the pinned half of a benchmark release (contract B)."""

from __future__ import annotations

from functools import cache

from nika.benchmark_engine.protocol import BenchmarkEngine


@cache
def benchmark_engine() -> BenchmarkEngine:
    """Return the engine that runs trials in this process.

    Only the in-tree engine exists so far; pinned benchmark engines select
    themselves from the release manifest once they run in their own container.
    """
    from nika.benchmark_engine.in_tree import InTreeBenchmarkEngine

    return InTreeBenchmarkEngine()
