"""Lifecycle of the opt-in resource monitoring stack.

``nika observability on`` starts cAdvisor and Prometheus for the whole host;
they keep running (and restart with Docker) until ``nika observability off``.
While the stack runs, every lab deployment starts this checkout's session
exporter in the background, and closing the last session stops it again.

Several checkouts can share one stack: each exporter binds a free port and
registers it in ``TARGETS_DIR``, which Prometheus watches as file discovery.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from nika.config import REPO_ROOT, RUNTIME_DIR, SESSIONS_DIR

logger = logging.getLogger(__name__)

COMPOSE_FILE = Path(__file__).resolve().parent / "stack" / "docker-compose.yml"
COMPOSE_PROJECT = "nika-metrics"
PROMETHEUS_URL = "http://127.0.0.1:9090"

# Host-wide state shared by every checkout that uses the stack.
METRICS_HOME = Path.home() / ".cache" / "nika" / "metrics"
TARGETS_DIR = METRICS_HOME / "targets"

# Per-checkout exporter state.
CHECKOUT_KEY = hashlib.sha1(str(REPO_ROOT).encode()).hexdigest()[:12]
EXPORTER_LOG = RUNTIME_DIR / "metrics" / "exporter.log"
EXPORTER_MODULE = "nika.metrics.exporter"

_EXPORTER_START_TIMEOUT_SEC = 15.0


def target_file(key: str = CHECKOUT_KEY) -> Path:
    return TARGETS_DIR / f"{key}.json"


def pid_file(key: str = CHECKOUT_KEY) -> Path:
    return TARGETS_DIR / f"{key}.pid"


@contextmanager
def _locked() -> Iterator[None]:
    """Serialize stack and exporter changes across processes and checkouts."""
    TARGETS_DIR.mkdir(parents=True, exist_ok=True)
    with open(METRICS_HOME / ".lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "NIKA_METRICS_TARGETS_DIR": str(TARGETS_DIR)}
    return subprocess.run(
        ["docker", "compose", "-p", COMPOSE_PROJECT, "-f", str(COMPOSE_FILE), *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def is_observability_on() -> bool:
    """True while the monitoring stack's containers are running."""
    from nika.runtime.shared.containers import docker_client

    try:
        running = docker_client().containers.list(
            filters={"label": f"com.docker.compose.project={COMPOSE_PROJECT}"}
        )
    except Exception:  # noqa: BLE001 — no Docker means no monitoring
        return False
    return len(running) >= 2


def _exporter_pid(key: str = CHECKOUT_KEY) -> int | None:
    """PID of a live exporter registered under ``key``, if any."""
    try:
        pid = int(pid_file(key).read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return None
    return pid if EXPORTER_MODULE.encode() in cmdline else None


def _stop_exporter(key: str) -> None:
    pid = _exporter_pid(key)
    if pid is not None:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while _exporter_pid(key) is not None and time.monotonic() < deadline:
            time.sleep(0.1)
    target_file(key).unlink(missing_ok=True)
    pid_file(key).unlink(missing_ok=True)


def observability_on() -> None:
    """Start cAdvisor and Prometheus; raise ``RuntimeError`` on failure."""
    with _locked():
        result = _compose("up", "-d", "--wait")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "docker compose up failed")
    if any(SESSIONS_DIR.glob("*.json")):
        ensure_exporter()


def observability_off(*, purge: bool = False) -> None:
    """Stop every registered exporter and the stack; keep data unless ``purge``."""
    with _locked():
        for path in TARGETS_DIR.glob("*.pid"):
            _stop_exporter(path.stem)
        result = _compose("down", *(["--volumes"] if purge else []))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "docker compose down failed")


def ensure_exporter() -> None:
    """Start this checkout's exporter in the background unless it already runs."""
    with _locked():
        if _exporter_pid() is not None:
            return
        target_file().unlink(missing_ok=True)
        EXPORTER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(EXPORTER_LOG, "a") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", EXPORTER_MODULE],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=REPO_ROOT,
                start_new_session=True,
            )
        pid_file().write_text(str(proc.pid))
        deadline = time.monotonic() + _EXPORTER_START_TIMEOUT_SEC
        while not target_file().exists():
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"NIKA metrics exporter failed; see {EXPORTER_LOG}")
            time.sleep(0.1)


def stop_exporter_if_idle() -> None:
    """Stop this checkout's exporter once no session of the checkout is running."""
    with _locked():
        if not any(SESSIONS_DIR.glob("*.json")):
            _stop_exporter(CHECKOUT_KEY)


def status() -> dict[str, object]:
    exporters = sorted(
        path.stem for path in TARGETS_DIR.glob("*.pid") if _exporter_pid(path.stem)
    )
    return {
        "on": is_observability_on(),
        "prometheus": PROMETHEUS_URL,
        "exporter_running": CHECKOUT_KEY in exporters,
        "exporters": len(exporters),
    }
