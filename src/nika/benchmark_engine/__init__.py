"""Benchmark engine: the half of a benchmark release that stays pinned (contract B)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from typing import Any

from nika.benchmark_engine.container import ENV_ENGINE_TOKEN
from nika.benchmark_engine.protocol import LifecycleProtocol

_active: LifecycleProtocol | None = None
# How spawned trial workers of a run reach its benchmark engine container.
ENV_ENGINE_URL = "NIKA_ENGINE_URL"


@cache
def _in_process() -> LifecycleProtocol:
    from nika.benchmark_engine.engine import BenchmarkEngine

    return BenchmarkEngine()


def benchmark_engine() -> LifecycleProtocol:
    """Return the lifecycle protocol that runs trials.

    That is the run's benchmark engine container when its release pins one,
    and otherwise the current NIKA's engine, called in process.
    """
    if _active is not None:
        return _active
    url = os.environ.get(ENV_ENGINE_URL)
    if url:
        return _client(url, os.environ.get(ENV_ENGINE_TOKEN, ""))
    return _in_process()


@cache
def _client(url: str, token: str) -> LifecycleProtocol:
    from nika.benchmark_engine.client import EngineClient

    return EngineClient(url, token=token)


@contextmanager
def use_engine(engine: LifecycleProtocol) -> Iterator[LifecycleProtocol]:
    """Make *engine* the one ``benchmark_engine`` returns, for one run."""
    global _active
    previous, _active = _active, engine
    try:
        yield engine
    finally:
        _active = previous


class EngineUnsupported(RuntimeError):
    """This run's execution settings cannot use a benchmark engine container."""


@contextmanager
def release_engine(
    pinned: dict[str, Any] | None, *, results_root: Path, name: str
) -> Iterator[dict[str, Any] | None]:
    """Run a release on the benchmark engine its manifest pins, if any.

    *pinned* is the manifest's ``engine`` block. While the context is open, a
    container built from ``engine.tag`` serves the run. Yields the engine's
    ``tag`` and ``image`` for the run's provenance, or ``None`` when the
    current NIKA's engine runs the release in process.
    """
    if pinned is None:
        yield None
        return
    from nika.firecracker.config import firecracker_enabled
    from nika.remote.config import is_remote_enabled

    if firecracker_enabled() or is_remote_enabled():
        raise EngineUnsupported(
            "Releases that pin a benchmark engine run on this host's Docker; "
            "Firecracker execution and NIKA Remote do not support them yet."
        )
    from nika.benchmark_engine.client import EngineClient
    from nika.benchmark_engine.container import engine_container, resolve_engine_image

    image = resolve_engine_image(pinned)
    with engine_container(image, run_id=name, results_root=results_root) as container:
        exported = {
            ENV_ENGINE_URL: container.base_url,
            ENV_ENGINE_TOKEN: container.token,
        }
        os.environ.update(exported)
        try:
            with use_engine(EngineClient(container.base_url, token=container.token)):
                yield {"tag": pinned.get("tag"), "image": container.image_id or image}
        finally:
            for key in exported:
                os.environ.pop(key, None)
