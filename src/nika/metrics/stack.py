"""Lifecycle of the opt-in resource monitoring stack.

``nika observability on`` starts cAdvisor, Prometheus, and Grafana for the
whole host; they keep running (and restart with Docker) until ``nika
observability off``. While the stack runs, every lab deployment and benchmark
run starts this checkout's exporter in the background. The exporter exits by
itself once the checkout has had no session and no live benchmark run for a
short grace period.

Several checkouts can share one stack: each exporter binds a free port and
registers it in ``TARGETS_DIR``, which Prometheus watches as file discovery.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from string import Template

import yaml

from nika.config import REPO_ROOT, RUNTIME_DIR, SESSIONS_DIR

logger = logging.getLogger(__name__)

STACK_DIR = Path(__file__).resolve().parent / "stack"
COMPOSE_FILE = STACK_DIR / "docker-compose.yml"
COMPOSE_PROJECT = "nika-metrics"
PROMETHEUS_URL = "http://127.0.0.1:9090"
GRAFANA_URL = "http://127.0.0.1:3000"
STACK_SERVICES = ("cadvisor", "node-exporter", "prometheus", "grafana")

# Host-wide state shared by every checkout that uses the stack.
METRICS_HOME = Path.home() / ".cache" / "nika" / "metrics"
TARGETS_DIR = METRICS_HOME / "targets"
# Prometheus configuration rendered by ``observability on``. It can hold the
# remote-write token, so it lives here rather than in the checkout.
PROMETHEUS_DIR = METRICS_HOME / "prometheus"
REMOTE_WRITE_TOKEN_ENV = "NIKA_METRICS_REMOTE_WRITE_TOKEN"
_TOKEN_FILE = "remote_write_token"
# Only NIKA's own series leave the host: recorded rules and exporter metrics.
_PUSHED_SERIES = "nika:.*|nika_.*"

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
def locked() -> Iterator[None]:
    """Serialize stack and exporter changes across processes and checkouts."""
    TARGETS_DIR.mkdir(parents=True, exist_ok=True)
    with open(METRICS_HOME / ".lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _scrape_interval() -> str:
    from nika.run_config.loader import get_run_config

    return get_run_config().nika.metrics.scrape_interval


def _seconds(duration: str) -> int:
    return 60 if duration == "1m" else int(duration[:-1])


def render_prometheus_config() -> None:
    """Write ``prometheus.yml`` and ``rules.yml`` for the configured settings.

    Raises ``RuntimeError`` when remote write is configured without a token.
    """
    from nika.run_config.loader import get_run_config

    settings = get_run_config().nika.metrics
    interval = settings.scrape_interval
    # ``rate()`` needs several samples in its window.
    rate_window = f"{max(60, 4 * _seconds(interval))}s"
    config: dict = {
        "global": {
            "scrape_interval": interval,
            "evaluation_interval": interval,
            # Sent with remote write only, to tell lab hosts apart.
            "external_labels": {"host": socket.gethostname()},
        },
        "rule_files": ["/etc/prometheus/nika/rules.yml"],
        "scrape_configs": [
            {
                "job_name": "cadvisor",
                "static_configs": [{"targets": ["127.0.0.1:8098"]}],
            },
            {
                "job_name": "node",
                "static_configs": [{"targets": ["127.0.0.1:9109"]}],
            },
            # One exporter per NIKA checkout, each registered in TARGETS_DIR.
            {
                "job_name": "nika",
                "file_sd_configs": [
                    {
                        "files": ["/etc/prometheus/targets/*.json"],
                        "refresh_interval": "30s",
                    }
                ],
            },
        ],
    }

    PROMETHEUS_DIR.mkdir(parents=True, exist_ok=True)
    PROMETHEUS_DIR.chmod(0o750)
    token_file = PROMETHEUS_DIR / _TOKEN_FILE
    token_file.unlink(missing_ok=True)
    remote = settings.remote_write
    if remote.url:
        token = os.environ.get(REMOTE_WRITE_TOKEN_ENV, "").strip()
        if not token:
            raise RuntimeError(
                f"nika.metrics.remote_write.url is set but {REMOTE_WRITE_TOKEN_ENV} "
                "is empty; add the token to .env"
            )
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(fd, "w") as handle:
            handle.write(token)
        secret = f"/etc/prometheus/nika/{_TOKEN_FILE}"
        auth = (
            {"basic_auth": {"username": remote.username, "password_file": secret}}
            if remote.username
            else {"authorization": {"credentials_file": secret}}
        )
        config["remote_write"] = [
            {
                "url": remote.url,
                **auth,
                "write_relabel_configs": [
                    {
                        "source_labels": ["__name__"],
                        "regex": _PUSHED_SERIES,
                        "action": "keep",
                    }
                ],
            }
        ]

    _write_text(
        PROMETHEUS_DIR / "prometheus.yml",
        "# Generated by `nika observability on`; edit config/nika.yaml instead.\n"
        + yaml.safe_dump(config, sort_keys=False),
    )
    rules = Template((STACK_DIR / "rules.yml").read_text(encoding="utf-8"))
    _write_text(PROMETHEUS_DIR / "rules.yml", rules.substitute(rate_window=rate_window))


def _write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o640)
    tmp.replace(path)


def _compose(*args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "NIKA_METRICS_TARGETS_DIR": str(TARGETS_DIR),
        "NIKA_METRICS_CONFIG_DIR": str(PROMETHEUS_DIR),
        "NIKA_METRICS_INTERVAL": _scrape_interval(),
        # Prometheus runs as nobody; this group lets it read the config files.
        "NIKA_METRICS_GID": str(os.getgid()),
    }
    return subprocess.run(
        ["docker", "compose", "-p", COMPOSE_PROJECT, "-f", str(COMPOSE_FILE), *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def is_observability_on() -> bool:
    """True while every service of the monitoring stack is running."""
    from nika.runtime.shared.containers import docker_client

    try:
        running = docker_client().containers.list(
            filters={"label": f"com.docker.compose.project={COMPOSE_PROJECT}"}
        )
    except Exception:  # noqa: BLE001 — no Docker means no monitoring
        return False
    services = {c.labels.get("com.docker.compose.service") for c in running}
    return services.issuperset(STACK_SERVICES)


def checkout_idle() -> bool:
    """True when this checkout has no session and no live benchmark run."""
    from nika.workflows.benchmark.run_progress import is_run_live, list_progress_docs

    if any(SESSIONS_DIR.glob("*.json")):
        return False
    return not any(is_run_live(doc) for doc in list_progress_docs())


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
    """Start the monitoring stack; raise ``RuntimeError`` on failure.

    Running it again applies changed settings to a running stack.
    """
    with locked():
        render_prometheus_config()
        result = _compose("up", "-d", "--wait")
        if result.returncode == 0:
            # A running Prometheus does not reread its mounted config by itself.
            result = _compose("kill", "-s", "SIGHUP", "prometheus")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "docker compose up failed")
    if not checkout_idle():
        ensure_exporter()


def observability_off(*, purge: bool = False) -> None:
    """Stop every registered exporter and the stack; keep data unless ``purge``."""
    with locked():
        for path in TARGETS_DIR.glob("*.pid"):
            _stop_exporter(path.stem)
        result = _compose("down", *(["--volumes"] if purge else []))
        # ``on`` writes the token again; nothing needs it while the stack is down.
        (PROMETHEUS_DIR / _TOKEN_FILE).unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "docker compose down failed")


def ensure_exporter() -> None:
    """Start this checkout's exporter in the background unless it already runs."""
    with locked():
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


def start_monitoring() -> bool:
    """Start the exporter if observability is on; return whether it runs.

    Raises ``RuntimeError`` when the stack is on but the exporter fails.
    """
    if not is_observability_on():
        return False
    ensure_exporter()
    return True


def release_if_idle() -> bool:
    """Deregister this checkout's exporter if the checkout is idle.

    Called by the exporter itself. Holding the lock means a deployment that
    starts concurrently either is seen here or starts a fresh exporter.
    """
    with locked():
        if not checkout_idle():
            return False
        target_file().unlink(missing_ok=True)
        if pid_file().is_file() and pid_file().read_text().strip() == str(os.getpid()):
            pid_file().unlink()
        return True


def status() -> dict[str, object]:
    exporters = sorted(
        path.stem for path in TARGETS_DIR.glob("*.pid") if _exporter_pid(path.stem)
    )
    from nika.run_config.loader import get_run_config

    return {
        "on": is_observability_on(),
        "prometheus": PROMETHEUS_URL,
        "grafana": GRAFANA_URL,
        "remote_write": get_run_config().nika.metrics.remote_write.url,
        "exporter_running": CHECKOUT_KEY in exporters,
        "exporters": len(exporters),
    }
