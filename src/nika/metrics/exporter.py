"""Prometheus exporter mapping lab containers to NIKA sessions.

Resource usage itself (CPU, memory, network) comes from cAdvisor, which sees
every Docker container regardless of backend. This exporter publishes the
missing link: which container belongs to which session, scenario, and node.
Prometheus joins both on the Docker container ``name`` label.

The exporter reads the local session store, so it must run on the lab host.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from prometheus_client import REGISTRY, start_http_server
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from nika.utils.session_store import SessionStore

logger = logging.getLogger(__name__)

# Lab containers do not change during a session, so ``clab inspect`` and
# Kathará lookups run at most once per TTL instead of on every scrape.
_CONTAINER_CACHE_TTL_SEC = 60.0

_SESSION_LABELS = (
    "session_id",
    "lab_name",
    "backend",
    "scenario",
    "topo_size",
    "agent_type",
    "model",
)
_CONTAINER_LABELS = ("session_id", "lab_name", "backend", "scenario", "node", "name")


def _start_timestamp(session: dict[str, Any]) -> float | None:
    raw = session.get("created_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


class SessionCollector(Collector):
    """Emit session and container info metrics for running NIKA sessions."""

    def __init__(self, store: SessionStore | None = None) -> None:
        self._store = store or SessionStore()
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
                    "lab_name": str(session.get("lab_name") or ""),
                    "backend": str(session.get("backend") or ""),
                    "scenario": str(session.get("scenario_name") or ""),
                    "topo_size": str(session.get("scenario_topo_size") or ""),
                    "agent_type": str(session.get("agent_type") or ""),
                    "model": str(session.get("model") or ""),
                }
                sessions_info.add_metric([labels[k] for k in _SESSION_LABELS], 1)
                started = _start_timestamp(session)
                if started is not None:
                    start_time.add_metric([session_id], started)

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

        errors.add_metric([], failed)
        running.add_metric([], live)
        yield from (sessions_info, start_time, containers_info, errors, running)


def serve_metrics(*, host: str, port: int) -> None:
    """Serve ``/metrics`` until interrupted."""
    REGISTRY.register(SessionCollector())
    server, thread = start_http_server(port, addr=host)
    logger.info("Serving NIKA metrics on http://%s:%d/metrics", host, port)
    try:
        while thread.is_alive():
            thread.join(timeout=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
