from __future__ import annotations

import fcntl
from pathlib import Path

import pytest

from tests.support.integration_pipeline import load_test_env
from tests.support.official_release import install as install_official_releases
from tests.support.prerequisites import docker_available
from tests.support.scenarios import register_test_scenarios

load_test_env()
register_test_scenarios()

_SANDBOX_E2E_LOCK = Path("/tmp/nika-sandbox-e2e.lock")


def _is_sandbox_e2e_test(node_path: str) -> bool:
    return (
        "test_sandbox_agents.py" in node_path
        or "test_sandbox_benchmark.py" in node_path
    )


@pytest.fixture(autouse=True)
def isolate_host_monitoring(request: pytest.FixtureRequest, monkeypatch, tmp_path):
    """Keep benchmark progress documents out of the checkout's ``runtime/``.

    Offline tests also must not start exporters on a host with observability on.
    """
    monkeypatch.setattr(
        "nika.workflows.benchmark.run_progress.BENCHMARK_RUNS_DIR",
        tmp_path / "benchmark_runs",
    )
    if not request.node.get_closest_marker(
        "e2e"
    ) and not request.node.get_closest_marker("integration"):
        monkeypatch.setattr("nika.metrics.stack.is_observability_on", lambda: False)
    yield


@pytest.fixture(autouse=True)
def official_releases(request: pytest.FixtureRequest, monkeypatch):
    """Serve official release tags from local files instead of GitHub.

    ``live`` tests read the real tags.
    """
    if request.node.get_closest_marker("live"):
        yield None
        return
    yield install_official_releases(monkeypatch)


@pytest.fixture(autouse=True)
def sandbox_e2e_serial(request: pytest.FixtureRequest):
    """Serialize sandbox E2E tests that share sbx / MCP gateway resources."""
    node_path = str(request.node.fspath)
    if not _is_sandbox_e2e_test(node_path):
        yield
        return

    _SANDBOX_E2E_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with _SANDBOX_E2E_LOCK.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _docker_unreachable_error(exc: BaseException) -> bool:
    """True when ``exc`` means the Docker daemon could not be contacted."""
    try:
        from docker.errors import DockerException
        from Kathara.exceptions import DockerDaemonConnectionError
    except ImportError:
        return False
    return isinstance(exc, (DockerDaemonConnectionError, DockerException))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item):
    return (yield from _skip_when_docker_unreachable())


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    return (yield from _skip_when_docker_unreachable())


def _skip_when_docker_unreachable():
    """Skip, not fail, tests that hit an unreachable Docker daemon.

    Several labs connect to Docker when constructed. Without a daemon those
    tests cannot run, so report them as skipped. With Docker up, errors pass
    through unchanged.
    """
    try:
        return (yield)
    except Exception as exc:
        if _docker_unreachable_error(exc) and not docker_available():
            pytest.skip(f"Docker daemon unavailable: {type(exc).__name__}")
        raise


def pytest_collection_modifyitems(config, items):
    """Require one explicit execution tier per collected test."""
    tiers = {"unit", "contract", "integration", "e2e"}
    invalid = []
    for item in items:
        marked = tiers.intersection(mark.name for mark in item.iter_markers())
        if len(marked) != 1:
            invalid.append(f"{item.nodeid}: {', '.join(sorted(marked)) or 'unmarked'}")
    if invalid:
        raise pytest.UsageError(
            "Tests need exactly one execution tier (unit, contract, integration, e2e):\n"
            + "\n".join(invalid)
        )
