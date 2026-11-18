"""Opt-in lifecycle timers. Nested intervals must not be added together.

Readiness predicates give observation bounds for address and protocol readiness.
They do not establish the exact instant an address or route was installed.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    destination = Path(os.environ["NIKA_HEALTHY_PHASE_LOG"])
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def wrap(owner, attribute, phase):
        original = getattr(owner, attribute, None)
        if original is None:
            return False

        @functools.wraps(original)
        def timed(*args, **kwargs):
            start, epoch = time.monotonic_ns(), time.time_ns()
            error = None
            result = None
            try:
                result = original(*args, **kwargs)
                return result
            except BaseException as exc:
                error = type(exc).__name__
                raise
            finally:
                record = {
                    "phase": phase,
                    "clock_domain": os.environ.get("NIKA_HEALTHY_CLOCK_DOMAIN", "host"),
                    "boot_id": boot_id,
                    "start_monotonic_ns": start,
                    "start_epoch_ns": epoch,
                    "duration_ns": time.monotonic_ns() - start,
                    "pid": os.getpid(),
                    "thread": threading.get_ident(),
                    "error": error,
                }
                if isinstance(result, dict) and "checks" in result:
                    record["checks"] = result["checks"]
                    record["verified"] = result.get("verified")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with _lock, destination.open("a") as output:
                    output.write(json.dumps(record) + "\n")

        setattr(owner, attribute, timed)
        return True

    import nika.workflows.env.start as start
    import nika.remote.handlers as handlers
    from docker.api.container import ContainerApiMixin
    from nika.runtime.kathara.runtime import KatharaRuntime
    from nika.runtime.containerlab.runtime import ContainerlabRuntime

    wrap(ContainerApiMixin, "start", "docker_start")
    wrap(KatharaRuntime, "deploy", "runtime_deploy")
    wrap(KatharaRuntime, "_wait_deploy_ready", "wait_containers_running")
    wrap(ContainerlabRuntime, "deploy", "runtime_deploy")
    wrap(start, "verify_lab_with_retry", "routing_and_health_verification")
    wrap(handlers, "handle_session_verify", "healthy_baseline_verification")
    wrap(start, "_configure_telemetry", "telemetry_setup")
    original_get = start.get_net_env_instance

    def get_environment(*args, **kwargs):
        env = original_get(*args, **kwargs)
        for attribute, phase in {
            "deploy": "environment_deploy",
            "_ensure_runtime_files": "configuration_generation",
            "_ensure_docker_images": "ensure_images",
            "preload_workload_images": "workload_image_preload",
            "sync_client_hosts": "sync_client_addresses",
            "post_deploy": "post_deploy",
            "start_business_workload": "business_workload_start",
            "startup_verify_lab": "readiness_observation",
            "verify_lab": "full_health_observation",
            "undeploy": "environment_teardown",
        }.items():
            wrap(env, attribute, phase)
        return env

    start.get_net_env_instance = get_environment
    wrap(start, "start_net_env", "start_net_env")
    timed_start = start.start_net_env

    @functools.wraps(timed_start)
    def observe_docker(*args, **kwargs):
        with tempfile.TemporaryFile(mode="w+") as events:
            since = str(int(time.time()) - 1)
            process = subprocess.Popen(
                [
                    "docker",
                    "events",
                    "--since",
                    since,
                    "--format",
                    "{{json .}}",
                    "--filter",
                    "type=container",
                    "--filter",
                    "event=start",
                    "--filter",
                    "event=die",
                    "--filter",
                    "event=exec_start",
                    "--filter",
                    "event=exec_die",
                ],
                stdout=events,
                stderr=subprocess.DEVNULL,
            )
            try:
                return timed_start(*args, **kwargs)
            finally:
                process.terminate()
                process.wait()
                events.seek(0)
                rows = [json.loads(line) for line in events if line.strip()]
                destination.parent.mkdir(parents=True, exist_ok=True)
                with _lock, destination.open("a") as output:
                    output.write(
                        json.dumps(
                            {
                                "phase": "docker_lifecycle_events",
                                "boot_id": boot_id,
                                "clock_domain": os.environ.get(
                                    "NIKA_HEALTHY_CLOCK_DOMAIN", "host"
                                ),
                                "pid": os.getpid(),
                                "events": rows,
                            }
                        )
                        + "\n"
                    )

    start.start_net_env = observe_docker
