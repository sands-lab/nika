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
import secrets
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
# Random Grafana admin password: shared run links expose this Grafana publicly.
GRAFANA_PASSWORD_FILE = METRICS_HOME / "grafana_admin_password"
_TOKEN_FILE = "remote_write_token"
# Only NIKA's own series leave the host: recorded rules and exporter metrics.
_PUSHED_SERIES = "nika:.*|nika_.*"

# Per-checkout exporter state.
CHECKOUT_KEY = hashlib.sha1(str(REPO_ROOT).encode()).hexdigest()[:12]
EXPORTER_LOG = RUNTIME_DIR / "metrics" / "exporter.log"
EXPORTER_MODULE = "nika.metrics.exporter"

_EXPORTER_START_TIMEOUT_SEC = 15.0
# A reload waits for the push queue to flush, up to Prometheus's one-minute
# flush deadline.
_RELOAD_TIMEOUT_SEC = 90.0


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
    # A token no longer needed is removed only after Prometheus has reloaded
    # (``observability_on``): its push queue still sends a last batch with it.
    token_file = PROMETHEUS_DIR / _TOKEN_FILE
    remote = settings.remote_write
    if remote.enabled:
        token = os.environ.get(REMOTE_WRITE_TOKEN_ENV, "").strip()
        if not token:
            raise RuntimeError(
                f"nika.metrics.remote_write is enabled but {REMOTE_WRITE_TOKEN_ENV} "
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


def _compose(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
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
        input=stdin,
    )


def service_states() -> dict[str, str]:
    """Docker state of each stack service (``missing`` when not created)."""
    from nika.runtime.shared.containers import docker_client

    states = dict.fromkeys(STACK_SERVICES, "missing")
    try:
        containers = docker_client().containers.list(
            all=True,
            filters={"label": f"com.docker.compose.project={COMPOSE_PROJECT}"},
        )
    except Exception:  # noqa: BLE001 — no Docker means no monitoring
        return states
    for container in containers:
        service = container.labels.get("com.docker.compose.service")
        if service in states:
            states[service] = container.status
    return states


def is_observability_on() -> bool:
    """True while every service of the monitoring stack is running."""
    return all(state == "running" for state in service_states().values())


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
    from nika.run_config.loader import get_run_config

    with locked():
        render_prometheus_config()
        result = _compose("up", "-d", "--wait")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "docker compose up failed")
        _reload_prometheus()
        if not get_run_config().nika.metrics.remote_write.enabled:
            (PROMETHEUS_DIR / _TOKEN_FILE).unlink(missing_ok=True)
        _set_grafana_admin_password()
    if not checkout_idle():
        ensure_exporter()


def grafana_admin_password() -> str:
    """The stack's Grafana admin password, created on first use."""
    try:
        return GRAFANA_PASSWORD_FILE.read_text().strip()
    except FileNotFoundError:
        pass
    METRICS_HOME.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(18)
    fd = os.open(GRAFANA_PASSWORD_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(password)
    return password


def _set_grafana_admin_password() -> None:
    """Replace Grafana's default ``admin`` password with the stack's own."""
    result = _compose(
        "exec",
        "-T",
        "grafana",
        "grafana",
        "cli",
        "admin",
        "reset-admin-password",
        "--password-from-stdin",
        stdin=grafana_admin_password() + "\n",
    )
    if result.returncode != 0:
        raise RuntimeError(
            result.stderr.strip() or "cannot set the Grafana admin password"
        )


def _reload_state() -> tuple[float, bool] | None:
    """Time and success of Prometheus's last config reload, if it answers."""
    import httpx

    try:
        text = httpx.get(f"{PROMETHEUS_URL}/metrics", timeout=5).text
    except httpx.HTTPError:
        return None
    values = {}
    for line in text.splitlines():
        name, _, value = line.partition(" ")
        if name.startswith("prometheus_config_last_reload_"):
            values[name] = float(value)
    try:
        return (
            values["prometheus_config_last_reload_success_timestamp_seconds"],
            values["prometheus_config_last_reload_successful"] == 1,
        )
    except KeyError:
        return None


def _reload_prometheus() -> None:
    """Make Prometheus reread its mounted config and wait until it has."""
    deadline = time.monotonic() + _RELOAD_TIMEOUT_SEC
    before = None
    while before is None and time.monotonic() < deadline:
        before = _reload_state()
        if before is None:
            time.sleep(0.5)
    if before is None:
        raise RuntimeError(f"Prometheus does not answer at {PROMETHEUS_URL}")
    result = _compose("kill", "-s", "SIGHUP", "prometheus")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "cannot signal Prometheus")
    while time.monotonic() < deadline:
        state = _reload_state()
        if state is not None and not state[1]:
            raise RuntimeError(
                "Prometheus rejected its configuration; "
                "see `docker logs nika-metrics-prometheus-1`"
            )
        if state is not None and state[0] > before[0]:
            return
        time.sleep(0.5)
    raise RuntimeError("Prometheus did not reload its configuration in time")


def observability_off(*, purge: bool = False) -> None:
    """Stop every registered exporter and the stack; keep data unless ``purge``."""
    from nika.metrics.share import stop_all_shares

    with locked():
        for path in TARGETS_DIR.glob("*.pid"):
            _stop_exporter(path.stem)
        # Shared links point at this Grafana; they would only show errors.
        stop_all_shares()
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

    settings = get_run_config().nika.metrics
    remote = settings.remote_write
    services = service_states()
    return {
        "on": all(state == "running" for state in services.values()),
        "services": services,
        "prometheus": PROMETHEUS_URL,
        "grafana": GRAFANA_URL,
        "grafana_password_file": str(GRAFANA_PASSWORD_FILE),
        "scrape_interval": settings.scrape_interval,
        "remote_write": remote.url if remote.enabled else None,
        "share": settings.share,
        "exporter_running": CHECKOUT_KEY in exporters,
        "exporters": len(exporters),
    }
