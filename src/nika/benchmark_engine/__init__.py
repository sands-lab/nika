"""Benchmark engine: the half of a benchmark release that stays pinned (contract B)."""

from __future__ import annotations

import json
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
# Set by ``nika engine serve``: the process runs environments, never agents.
ENV_ENGINE_SERVER = "NIKA_ENGINE_SERVER"


def is_engine_server() -> bool:
    """True inside a benchmark engine container's service."""
    return os.environ.get(ENV_ENGINE_SERVER) == "1"


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


def recorded_engine(trial_dir: str | Path) -> dict[str, Any] | None:
    """The pinned benchmark engine that ran the trial in *trial_dir*, if any.

    A run records the engine's ``tag`` and ``image`` in the provenance of the
    ``run.json`` next to its ``trials`` directory.
    """
    path = Path(trial_dir)
    if path.parent.name != "trials":
        return None
    try:
        run = json.loads((path.parent.parent / "run.json").read_text("utf-8"))
        engine = run["provenance"]["engine"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(engine, dict) or not engine.get("image"):
        return None
    return engine


def stop_recorded_trial(
    session_id: str, trial_dir: str | Path | None, *, status: str = "finished"
) -> bool:
    """Tear a trial down through the pinned benchmark engine that ran it.

    The engine container exits with its run, so this starts it again from
    the image the run recorded. Returns False when no pinned engine ran the
    trial, so the caller closes it in this process.
    """
    engine = recorded_engine(trial_dir) if trial_dir else None
    if engine is None:
        return False
    from nika.benchmark_engine.client import EngineClient
    from nika.benchmark_engine.container import engine_container, resolve_engine_image
    from nika.benchmark_engine.protocol import TrialHandle

    image = resolve_engine_image(engine)
    run_root = Path(str(trial_dir)).parent.parent
    name = f"close-{engine.get('release') or 'run'}"
    with engine_container(image, run_id=name, results_root=run_root) as container:
        EngineClient(container.base_url, token=container.token).stop_trial(
            TrialHandle(session_id=session_id, trial_dir=str(trial_dir)),
            status=status,
        )
    return True
