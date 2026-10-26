"""Prometheus exporter mapping lab containers to NIKA sessions and runs.

Resource usage itself (CPU, memory, network) comes from cAdvisor, which sees
every Docker container regardless of backend. This exporter publishes the
missing links: which container belongs to which session, scenario, node, and
benchmark run. Prometheus joins both on the Docker container ``name`` label.
It also publishes the trial counts of benchmark runs, read from their progress
documents under ``runtime/benchmark_runs/``.

The exporter reads the local session store, so it runs on the lab host, one
per checkout. ``nika.metrics.stack`` starts it; it exits by itself once the
checkout stays idle.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from nika.config import REPO_ROOT
from nika.utils.session_store import SessionStore
from nika.workflows.benchmark.run_progress import (
    is_run_live,
    list_progress_docs,
    run_display_name,
)

logger = logging.getLogger(__name__)

# Lab containers do not change during a session, so ``clab inspect`` and
# Kathará lookups run at most once per TTL instead of on every scrape.
_CONTAINER_CACHE_TTL_SEC = 60.0
# Finished runs stay exported this long, so their final counts are scraped.
_RECENT_RUN_SEC = 600.0
# The exporter exits after the checkout has been idle this long; the grace
# keeps it alive between the trials of a benchmark and leaves time for the
# final counts to be scraped.
_IDLE_GRACE_SEC = 120.0
_IDLE_CHECK_SEC = 5.0

_SESSION_LABELS = (
    "session_id",
    "run_id",
    "lab_name",
    "backend",
    "scenario",
    "topo_size",
    "agent_type",
    "model",
)
_CONTAINER_LABELS = (
    "session_id",
    "run_id",
    "lab_name",
    "backend",
    "scenario",
    "node",
    "name",
)
_RUN_LABELS = (
    "run_id",
    "run_name",
    "benchmark_id",
    "version",
    "agent_type",
    "model",
    "status",
)


def _timestamp(raw: object) -> float | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except ValueError:
        return None


_Run = tuple[Path, str, bool, float]  # result dir, run id, live, start time


def _session_run_id(session: dict[str, Any], runs: list[_Run]) -> str:
    """Benchmark run whose results hold the session, else the stamped run id.

    The result directory is known from deployment on, while a release run
    stamps ``benchmark_run_id`` only after the lab is up; preferring it keeps
    one ``run_id`` label for the whole session. Several runs can name the same
    directory, for example a run started again after its results were
    deleted: the most specific directory wins, then a live run, then the
    latest start.
    """
    session_dir = session.get("session_dir")
    if session_dir:
        path = Path(str(session_dir)).resolve()
        matches = [run for run in runs if path.is_relative_to(run[0])]
        if matches:
            best = max(matches, key=lambda run: (len(run[0].parts), run[2], run[3]))
            return best[1]
    return str(session.get("benchmark_run_id") or "")


class SessionCollector(Collector):
    """Emit info metrics for running sessions and benchmark run progress."""

    def __init__(
        self,
        store: SessionStore | None = None,
        *,
        runs_dir: Path | None = None,
    ) -> None:
        self._store = store or SessionStore()
        self._runs_dir = runs_dir
        self._lock = threading.Lock()
        self._containers: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    def _session_containers(self, session_id: str) -> list[dict[str, Any]]:
        from nika.workflows.session.containers import list_session_containers

        now = time.monotonic()
        cached = self._containers.get(session_id)
        if cached and cached[1] and now - cached[0] < _CONTAINER_CACHE_TTL_SEC:
            return cached[1]
        _, _, rows = list_session_containers(session_id, store=self._store)
        self._containers[session_id] = (now, rows)
        return rows

    def collect(self) -> Iterator[GaugeMetricFamily]:
        sessions_info = GaugeMetricFamily(
            "nika_session_info",
            "Running NIKA session; join on session_id.",
            labels=_SESSION_LABELS,
        )
        start_time = GaugeMetricFamily(
            "nika_session_start_time_seconds",
            "Unix time at which the session was created.",
            labels=("session_id",),
        )
        containers_info = GaugeMetricFamily(
            "nika_container_info",
            "Lab container of a running session; join cAdvisor metrics on name.",
            labels=_CONTAINER_LABELS,
        )
        errors = GaugeMetricFamily(
            "nika_session_discovery_errors",
            "Running sessions whose containers could not be listed in this scrape.",
        )
        running = GaugeMetricFamily(
            "nika_running_sessions", "Number of running NIKA sessions."
        )
        runs_info = GaugeMetricFamily(
            "nika_benchmark_run_info",
            "Benchmark run that is live or finished recently; join on run_id.",
            labels=_RUN_LABELS,
        )
        trials = GaugeMetricFamily(
            "nika_benchmark_trials",
            "Trials of a benchmark run by state (total, completed, pending, running).",
            labels=("run_id", "state"),
        )
        run_start = GaugeMetricFamily(
            "nika_benchmark_run_start_time_seconds",
            "Unix time at which this launch of the benchmark run started.",
            labels=("run_id",),
        )
        run_update = GaugeMetricFamily(
            "nika_benchmark_run_last_update_time_seconds",
            "Unix time of the run's last progress update.",
            labels=("run_id",),
        )

        docs = list_progress_docs(runs_dir=self._runs_dir)
        runs = [
            (
                Path(str(doc["result_dir"])).resolve(),
                str(doc["run_id"]),
                is_run_live(doc),
                _timestamp(doc.get("started_at")) or 0.0,
            )
            for doc in docs
        ]
        running_by_run: dict[str, int] = {}

        with self._lock:
            rows = self._store.list_running_sessions()
            live_ids = {row["session_id"] for row in rows}
            for stale in set(self._containers) - live_ids:
                del self._containers[stale]

            failed = live = 0
            for row in rows:
                session_id = row["session_id"]
                try:
                    session = self._store.get_session(session_id)
                except FileNotFoundError:
                    continue
                live += 1
                labels = {
                    "session_id": session_id,
                    "run_id": _session_run_id(session, runs),
                    "lab_name": str(session.get("lab_name") or ""),
                    "backend": str(session.get("backend") or ""),
                    "scenario": str(session.get("scenario_name") or ""),
                    "topo_size": str(session.get("scenario_topo_size") or ""),
                    "agent_type": str(session.get("agent_type") or ""),
                    "model": str(session.get("model") or ""),
                }
                if labels["run_id"]:
                    running_by_run[labels["run_id"]] = (
                        running_by_run.get(labels["run_id"], 0) + 1
                    )
                sessions_info.add_metric([labels[k] for k in _SESSION_LABELS], 1)
                started = _timestamp(session.get("created_at"))
                if started is not None:
                    start_time.add_metric([session_id], started)

                if session.get("remote"):
                    # The lab runs on a remote host or in a Firecracker
                    # microVM, out of sight of this host's cAdvisor.
                    continue
                try:
                    containers = self._session_containers(session_id)
                except Exception as exc:  # noqa: BLE001 — one bad session must not fail the scrape
                    failed += 1
                    logger.warning(
                        "Cannot list containers of session %s: %s", session_id, exc
                    )
                    continue
                for container in containers:
                    container_labels = {
                        **labels,
                        "node": str(container.get("name") or ""),
                        "name": str(container.get("container_name") or ""),
                    }
                    containers_info.add_metric(
                        [container_labels[k] for k in _CONTAINER_LABELS], 1
                    )

        now = time.time()
        for doc in docs:
            run_id = str(doc["run_id"])
            updated = _timestamp(doc.get("updated_at"))
            live_run = is_run_live(doc)
            if not live_run and (updated is None or now - updated > _RECENT_RUN_SEC):
                continue
            status = str(doc.get("status") or "unknown")
            if status == "running" and not live_run:
                status = "stopped"
            run_name = doc.get("run_name") or run_display_name(
                doc.get("model"), doc.get("agent_type"), doc.get("started_at")
            )
            runs_info.add_metric(
                [
                    run_id,
                    str(run_name),
                    str(doc.get("benchmark_id") or ""),
                    str(doc.get("version") or ""),
                    str(doc.get("agent_type") or ""),
                    str(doc.get("model") or ""),
                    status,
                ],
                1,
            )
            for state in ("total", "completed", "pending"):
                trials.add_metric([run_id, state], int(doc.get(f"{state}_trials") or 0))
            trials.add_metric([run_id, "running"], running_by_run.get(run_id, 0))
            started = _timestamp(doc.get("started_at"))
            if started is not None:
                run_start.add_metric([run_id], started)
            if updated is not None:
                run_update.add_metric([run_id], updated)

        errors.add_metric([], failed)
        running.add_metric([], live)
        yield from (
            sessions_info,
            start_time,
            containers_info,
            errors,
            running,
            runs_info,
            trials,
            run_start,
            run_update,
        )


def main() -> None:
    """Run the exporter as a background process started by ``ensure_exporter``.

    Binds a free localhost port and registers it for Prometheus file
    discovery; the registration is removed on SIGTERM or once idle.
    """
    from nika.metrics.stack import (
        checkout_idle,
        pid_file,
        release_if_idle,
        target_file,
    )

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    REGISTRY.register(SessionCollector())
    server, thread = start_http_server(0, addr="127.0.0.1")
    port = server.server_address[1]
    target = target_file()
    tmp = target.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            [{"targets": [f"127.0.0.1:{port}"], "labels": {"checkout": str(REPO_ROOT)}}]
        )
    )
    tmp.replace(target)
    logger.info("Serving NIKA metrics on http://127.0.0.1:%d/metrics", port)

    def _stop(*_: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    idle_since: float | None = None
    try:
        while thread.is_alive():
            thread.join(timeout=_IDLE_CHECK_SEC)
            if not checkout_idle():
                idle_since = None
                continue
            idle_since = idle_since or time.monotonic()
            if time.monotonic() - idle_since >= _IDLE_GRACE_SEC and release_if_idle():
                logger.info("No session or benchmark run left; exiting")
                return
    finally:
        target.unlink(missing_ok=True)
        try:
            if pid_file().read_text().strip() == str(os.getpid()):
                pid_file().unlink()
        except OSError:
            pass
        server.shutdown()


if __name__ == "__main__":
    main()
