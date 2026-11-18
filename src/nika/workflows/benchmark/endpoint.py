"""Pause trial admission while the configured model cannot answer."""

from __future__ import annotations

import logging
import time

from nika.run_config.schema import BenchmarkSettings


class EndpointUnavailable(RuntimeError):
    pass


class EndpointBackoff:
    def __init__(self, settings: BenchmarkSettings, probe):
        self.settings = settings
        self.probe = probe
        self.failures = 0
        self.deadline: float | None = None
        self.next_probe = 0.0
        self.delay = 0.0

    def record(self, outcome: str | None) -> None:
        if outcome == "endpoint_failed":
            self.failures += 1
            if self.paused and self.deadline is None:
                now = time.monotonic()
                self.deadline = now + self.settings.endpoint_wait_timeout_sec
                self.delay = min(
                    self.settings.endpoint_backoff_initial_sec,
                    self.settings.endpoint_backoff_max_sec,
                )
                self.next_probe = now + self.delay
                logging.getLogger(__name__).warning(
                    "Pausing trial admission while the model endpoint recovers."
                )
        elif outcome in {"success", "agent_failed"}:
            self.failures = 0
            self.deadline = None

    @property
    def paused(self) -> bool:
        return self.failures >= self.settings.endpoint_failure_threshold

    def _remaining(self) -> float:
        assert self.deadline is not None
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise EndpointUnavailable(
                "Model endpoint did not recover before the benchmark wait limit; resume the run after recovery."
            )
        return remaining

    def poll(self) -> None:
        if not self.paused:
            return
        remaining = self._remaining()
        if time.monotonic() < self.next_probe:
            return
        try:
            self.probe(min(10.0, remaining))
        except Exception:
            self._remaining()
            self.delay = min(self.delay * 2, self.settings.endpoint_backoff_max_sec)
            self.next_probe = time.monotonic() + self.delay
        else:
            self._remaining()
            self.failures = 0
            self.deadline = None
            logging.getLogger(__name__).warning(
                "Model endpoint recovered; resuming trial admission."
            )

    def wait(self) -> None:
        while self.paused:
            self.poll()
            if self.paused:
                time.sleep(
                    min(self._remaining(), max(0, self.next_probe - time.monotonic()))
                )


def _probe_worker(agent_type, provider, model, env_file, base_url, timeout) -> None:
    import os
    from pathlib import Path
    from nika.agents.endpoint_probe import probe_agent_generation

    if provider == "custom" and base_url:
        os.environ["NIKA_CUSTOM_BASE_URL"] = base_url
    try:
        probe_agent_generation(
            agent_type, provider, model, env_file=Path(env_file), timeout=timeout
        )
    except Exception:
        # Provider errors can include credentials; the scheduler only needs health.
        raise SystemExit(1) from None


def probe_model(
    provider: str | None,
    model: str | None,
    timeout: float,
    *,
    agent_type: str | None = None,
) -> None:
    import multiprocessing
    from nika.agents.sandbox.config import project_credentials_env_file
    from nika.run_config.loader import get_run_config
    from nika.utils.agent_config import resolve_agent_model

    config = get_run_config()
    agent_type = agent_type or config.agent.type
    worker = multiprocessing.get_context("spawn").Process(
        target=_probe_worker,
        args=(
            agent_type,
            provider or config.agent.provider,
            resolve_agent_model(agent_type, model, config=config),
            str(project_credentials_env_file()),
            config.agent.base_url,
            timeout,
        ),
        name="nika-endpoint-probe",
    )
    worker.start()
    try:
        worker.join(timeout)
        if worker.is_alive() or worker.exitcode != 0:
            raise EndpointUnavailable(
                "The model endpoint probe did not complete successfully."
            )
    finally:
        if worker.is_alive():
            worker.terminate()
            worker.join(1)
        if worker.is_alive():
            worker.kill()
            worker.join()
        worker.close()
