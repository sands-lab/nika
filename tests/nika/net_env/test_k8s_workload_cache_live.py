"""Live Docker test for Kubernetes workload image cache speedup."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from nika.net_env.utils.k8s_workload_cache import (
    cache_root,
    cache_scenario,
    cached_tar_paths,
)
from nika.utils.session_id import resolve_session_tag
from nika.workflows.session.close import close_session
from nika.workflows.session.list import list_sessions
from tests.support.prerequisites import docker_available, privileged_lab_supported

# Hard ceiling per deploy (verify + bootstrap). Successful llmd_lab runs ~2-3 min.
_DEPLOY_HARD_TIMEOUT_SEC = 900.0
# Tighter verify budget for tests so a dead controller fails in ~10 min, not 30.
_TEST_VERIFY_MAX_WAIT_SEC = 600.0


def _preflight(instance_tag: str) -> None:
    if not docker_available():
        pytest.skip("Docker unavailable")
    if not privileged_lab_supported():
        pytest.skip("Privileged k3s containers require Docker access")

    try:
        with open("/proc/sys/fs/file-nr", encoding="ascii") as handle:
            allocated = int(handle.read().split()[0])
    except OSError:
        allocated = 0
    if allocated > 500_000:
        pytest.skip(
            f"Host open file count too high for k3s deploy ({allocated}); retry later"
        )

    for row in list_sessions(running_only=True):
        lab_name = str(row.get("lab_name") or "")
        if instance_tag in lab_name:
            close_session(session_id=row["session_id"], undeploy=True)


def _close_sessions_for_instance_tag(instance_tag: str) -> None:
    for row in list_sessions(running_only=True):
        lab_name = str(row.get("lab_name") or "")
        if instance_tag in lab_name:
            close_session(session_id=row["session_id"], undeploy=True)


@contextmanager
def _hidden_workload_cache():
    """Temporarily hide host tar caches so preload cannot sideload into k3s."""
    root = cache_root()
    hidden: Path | None = None
    if root.exists():
        hidden = root.parent / f"k8s-images-hidden-{uuid4().hex[:8]}"
        root.rename(hidden)
    try:
        yield
    finally:
        if hidden is not None and hidden.exists():
            if root.exists():
                shutil.rmtree(root)
            hidden.rename(root)


def _deploy_once(
    scenario: str,
    *,
    instance_tag: str,
    session_tag: str,
    timeout_sec: float = _DEPLOY_HARD_TIMEOUT_SEC,
) -> tuple[str, float]:
    """Deploy in a subprocess with a hard timeout and always cleanup on failure."""
    payload = json.dumps(
        {
            "scenario": scenario,
            "instance_tag": instance_tag,
            "session_tag": session_tag,
        }
    )
    script = f"""
import json, sys, time
from nika.net_env.k8s_lab.lab import K8sFatTreeBGP
from nika.net_env.llmd_lab.lab import LLMDInferenceCluster
from nika.workflows.env.start import start_net_env

LLMDInferenceCluster.VERIFY_MAX_WAIT_SEC = {_TEST_VERIFY_MAX_WAIT_SEC}
K8sFatTreeBGP.VERIFY_MAX_WAIT_SEC = {_TEST_VERIFY_MAX_WAIT_SEC}

payload = json.loads({payload!r})
start = time.monotonic()
session_id = start_net_env(
    payload["scenario"],
    None,
    redeploy=True,
    instance_tag=payload["instance_tag"],
    session_tag=payload["session_tag"],
)
elapsed = time.monotonic() - start
print(json.dumps({{"session_id": session_id, "elapsed": elapsed}}))
"""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as exc:
        _close_sessions_for_instance_tag(instance_tag)
        raise RuntimeError(
            f"{scenario} deploy exceeded {timeout_sec:.0f}s hard timeout"
        ) from exc
    except subprocess.CalledProcessError as exc:
        _close_sessions_for_instance_tag(instance_tag)
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"{scenario} deploy failed: {detail}") from exc

    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    result = json.loads(lines[-1])
    return str(result["session_id"]), float(result["elapsed"])


def _run_cold_hot_timing(scenario: str) -> tuple[float, float]:
    instance_tag = f"cache-test-{uuid4().hex[:8]}"
    _preflight(instance_tag)
    cache_scenario(scenario)
    if not cached_tar_paths(scenario):
        pytest.skip(f"No workload image tars cached for {scenario}")

    session_ids: list[str] = []
    session_tag = resolve_session_tag(context="test")
    try:
        with _hidden_workload_cache():
            session_id, cold_elapsed = _deploy_once(
                scenario,
                instance_tag=instance_tag,
                session_tag=session_tag,
            )
            session_ids.append(session_id)
            close_session(session_id=session_id, undeploy=True)

        session_id, hot_elapsed = _deploy_once(
            scenario,
            instance_tag=instance_tag,
            session_tag=session_tag,
        )
        session_ids.append(session_id)

        return cold_elapsed, hot_elapsed
    finally:
        for session_id in reversed(session_ids):
            close_session(session_id=session_id, undeploy=True)
        _close_sessions_for_instance_tag(instance_tag)


def _assert_hot_faster(
    cold_elapsed: float, hot_elapsed: float, *, scenario: str
) -> None:
    saved = cold_elapsed - hot_elapsed
    # Allow small noise when both paths are already fast on this host.
    min_saved = max(5.0, cold_elapsed * 0.05)
    assert saved >= min_saved, (
        f"Expected {scenario} cached sideload to save at least {min_saved:.1f}s "
        f"(cold={cold_elapsed:.1f}s hot={hot_elapsed:.1f}s saved={saved:.1f}s)"
    )


@pytest.mark.live
@pytest.mark.integration
@pytest.mark.skipif(
    not (docker_available() and privileged_lab_supported()),
    reason="Requires Docker and root (privileged k3s containers)",
)
def test_llmd_lab_redeploy_is_faster_with_cached_workload_images(capsys) -> None:
    cold_elapsed, hot_elapsed = _run_cold_hot_timing("llmd_lab")
    saved = cold_elapsed - hot_elapsed
    message = (
        f"llmd_lab timing: cold={cold_elapsed:.1f}s "
        f"hot={hot_elapsed:.1f}s saved={saved:.1f}s"
    )
    print(message)
    with capsys.disabled():
        print(message)
    _assert_hot_faster(cold_elapsed, hot_elapsed, scenario="llmd_lab")


@pytest.mark.live
@pytest.mark.integration
@pytest.mark.skipif(
    not (docker_available() and privileged_lab_supported()),
    reason="Requires Docker and root (privileged k3s containers)",
)
def test_k8s_lab_redeploy_is_faster_with_cached_workload_images(capsys) -> None:
    cold_elapsed, hot_elapsed = _run_cold_hot_timing("k8s_lab")
    saved = cold_elapsed - hot_elapsed
    message = (
        f"k8s_lab timing: cold={cold_elapsed:.1f}s "
        f"hot={hot_elapsed:.1f}s saved={saved:.1f}s"
    )
    print(message)
    with capsys.disabled():
        print(message)
    _assert_hot_faster(cold_elapsed, hot_elapsed, scenario="k8s_lab")
