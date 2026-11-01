"""Benchmark engine: the half of a benchmark release that stays pinned (contract B)."""

from __future__ import annotations

from functools import cache

from nika.benchmark_engine.protocol import LifecycleProtocol


@cache
def benchmark_engine() -> LifecycleProtocol:
    """Return the lifecycle protocol that runs trials.

    Only the current NIKA's engine, called in process, exists so far. Releases
    that pin an engine will get a client to that engine's container.
    """
    from nika.benchmark_engine.engine import BenchmarkEngine

    return BenchmarkEngine()
