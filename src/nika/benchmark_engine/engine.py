"""Benchmark engine: deploys, injects, serves tools to, and scores trials.

Releases whose manifest pins no ``engine`` run on it in process; a release's
engine container serves the same class. The wrapped workflows already forward
to NIKA Remote when it is enabled. Each trial's MCP gateway runs here too, so
everything the agent observes comes from the release.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from nika.config import BENCHMARK_VERSION
from nika.firecracker.trial import active_trial_lab
from nika.mcp.gateway.lifecycle import SANDBOX_GATEWAY_AGENT_HOST, GatewayLeases
from nika.net_env.net_env_pool import scenario_requires_topo_size
from nika.benchmark_engine.host import engine_host_info
from nika.benchmark_engine.protocol import (
    API_VERSIONS,
    AgentEndpoints,
    AgentTask,
    EngineDescription,
    EngineError,
    FaultCheck,
    GatewayRequest,
    Resource,
    TrialHandle,
    TrialRequest,
)
from nika.utils.logger import bind_session_dir
from nika.utils.session import Session
from nika.validation.presence import (
    anomaly_expected,
    bound_injected_problem,
    log_presence_check,
    presence_record,
    recheck_bound_artifact,
    remote_lab,
)
from nika.workflows.env.start import start_net_env
from nika.workflows.eval.session import run_eval_metrics
from nika.workflows.session.close import close_session, load_session_meta_for_close

# Commit an engine image was built from (see ``docker/Dockerfile``).
ENV_ENGINE_COMMIT = "NIKA_ENGINE_COMMIT"

# Task-row keys forwarded to ``start_net_env`` as deployment parameters.
_DEPLOY_KEYS = ("topo", "igp", "bgp_mode", "rpki", "backend", "device_profile")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BenchmarkEngine:
    """Runs this NIKA's environments, failures, MCP gateway, and scoring."""

    def __init__(self) -> None:
        self._gateways = GatewayLeases()
        self._schedulers: dict[str, object] = {}
        # Started trials: the request ``begin`` prepares, and whether the
        # environment came up with its failure already injected.
        self._started: dict[str, tuple[TrialRequest, bool]] = {}

    def describe(self, release: str | None = None) -> EngineDescription:
        """Describe *release*; this engine serves every release NIKA ships."""
        from nika.workflows.benchmark.release import load_release, read_git_commit

        release = load_release(release or BENCHMARK_VERSION)
        commit, dirty = read_git_commit()
        if commit is None:
            # An engine image: built from a clean commit, without ``.git``.
            commit = os.environ.get(ENV_ENGINE_COMMIT) or None
        return EngineDescription(
            api_versions=sorted(API_VERSIONS),
            release_id=release.id,
            release_version=release.version,
            engine_commit=commit,
            engine_dirty=dirty,
            task_file_sha256={
                str(meta["cases_file"]): _sha256(release.root / meta["cases_file"])
                for meta in release.splits.values()
            },
            host=engine_host_info(),
        )

    def start_trial(self, request: TrialRequest) -> TrialHandle:
        task = request.task
        scenario = task["scenario"]
        size = (
            task.get("topo_size") or None
            if scenario_requires_topo_size(scenario)
            else None
        )
        from nika.schedule import schedule_for_task

        plan = schedule_for_task(task, request.release)
        image_lab = active_trial_lab()
        if image_lab is not None:
            # Firecracker execution: the lab was restored from a lab image.
            session_id = image_lab.attach(
                session_id=request.session_id,
                result_dir=request.result_dir,
                session_dir=request.trial_dir,
            )
        else:
            session_id = start_net_env(
                scenario,
                size,
                redeploy=True,
                result_dir=request.result_dir,
                session_tag=request.session_tag,
                session_id=request.session_id,
                session_dir=request.trial_dir,
                release=request.release,
                telemetry=task.get("telemetry"),
                traffic=task.get("traffic"),
                **{key: task.get(key) for key in _DEPLOY_KEYS},
                **(
                    {"defer_background_traffic": True}
                    if plan.activates("background")
                    else {}
                ),
                **(
                    {"defer_business_traffic": True}
                    if plan.activates("business")
                    else {}
                ),
            )
        trial_dir = request.trial_dir
        try:
            if request.release:
                # Injection resolves the release's failure variants from it.
                Session().load_running_session(session_id=session_id).update_session(
                    "benchmark_version", request.release
                )
            if trial_dir is None:
                trial_dir = str(load_session_meta_for_close(session_id)["session_dir"])
        except BaseException:
            close_session(session_id=session_id, undeploy=True, status="error")
            raise
        preinjected = image_lab is not None and image_lab.case is not None
        self._started[session_id] = (request, preinjected)
        return TrialHandle(
            session_id=session_id,
            trial_dir=trial_dir,
            resources=[Resource(kind="nika_session", id=session_id)],
        )

    def begin(self, handle: TrialHandle) -> AgentTask:
        """Prepare the task's timeline and its initial agent description.

        The frontend opens the gateway before releasing the timeline through
        schedule_operation. The engine owns injection, traffic, and recovery.
        """
        session_id = handle.session_id
        try:
            request, preinjected = self._started[session_id]
        except KeyError:
            raise EngineError(
                f"trial {session_id} was not started by this engine"
            ) from None
        from nika.remote.config import is_remote_enabled
        from nika.remote.schedule import RemoteSessionScheduler
        from nika.schedule import schedule_for_task
        from nika.workflows.schedule import SessionScheduler

        scheduler_type = (
            RemoteSessionScheduler if is_remote_enabled() else SessionScheduler
        )
        scheduler = scheduler_type(
            session_id,
            schedule_for_task(request.task, request.release),
            traffic=request.task.get("traffic"),
            expected_root_causes=request.expected_root_causes,
            restored=preinjected,
        )
        self._schedulers[session_id] = scheduler
        scheduler.prepare()
        session = Session().load_running_session(session_id=session_id)
        task_description = getattr(session, "task_description", None)
        if not task_description:
            raise EngineError(f"trial {session_id} has no task description")
        return AgentTask(task_description=task_description, scheduled=True)

    def schedule_operation(self, handle: TrialHandle, operation: str) -> dict:
        """Drive or observe the timeline on the engine's lab controller."""
        scheduler = self._schedulers[handle.session_id]
        if operation == "start":
            scheduler.start()
        elif operation == "agent-ready":
            scheduler.wait_agent_start()
            if (
                scheduler.plan.static
                and bound_injected_problem(handle.session_id) is None
            ):
                bind_session_dir(handle.trial_path)
                log_presence_check(
                    presence_record(self.fault_check(handle), "post_inject")
                )
        elif operation == "close":
            scheduler.close()
        elif operation != "status":
            raise ValueError(f"Unknown schedule operation: {operation}")
        return scheduler.status()

    def agent_endpoints(
        self, handle: TrialHandle, request: GatewayRequest
    ) -> AgentEndpoints:
        """Open the trial's MCP gateway; it serves this release's tools.

        With NIKA Remote, the lab host runs the gateway next to the lab.
        """
        from nika.mcp.gateway.phase import phase_advance_token
        from nika.mcp.registry import SUBMISSION_SERVER, select_diagnosis_servers
        from nika.mcp.gateway.session_registry import get_session
        from nika.runtime.factory import resolve_backend
        from nika.runtime.meta import device_profile_for
        from nika.telemetry import telemetry_for_session
        from nika.utils.session_store import SessionStore

        session_id = handle.session_id
        row = SessionStore().get_session(session_id)
        scenario = str(row.get("scenario_name") or "")
        backend = resolve_backend(row)
        servers = select_diagnosis_servers(
            scenario,
            backend=backend,
            telemetry=telemetry_for_session(row),
            device_profile=device_profile_for(row),
        )
        if SUBMISSION_SERVER not in servers:
            servers.append(SUBMISSION_SERVER)
        if remote_lab():
            from nika.remote.client import RemoteClient

            attach = RemoteClient().mcp_attach(
                session_id,
                access=request.access,
                tool_call_limits=request.tool_call_limits,
            )
            return AgentEndpoints(
                gateway_url=attach.gateway_base_url,
                agent_url=attach.gateway_base_url,
                session_token=attach.session_token,
                phase_token=attach.phase_token,
                servers=servers,
            )
        manager = self._gateways.open(
            session_id,
            scenario_name=scenario,
            host=request.bind_host,
            sandbox=request.sandbox,
            sandbox_agent_host=request.agent_host or SANDBOX_GATEWAY_AGENT_HOST,
            backend=backend,
            access=request.access,
            tool_call_limits=request.tool_call_limits,
        )
        return AgentEndpoints(
            gateway_url=manager.base_url,
            agent_url=manager.agent_url or manager.base_url,
            session_token=get_session(session_id).access_token,
            phase_token=phase_advance_token(session_id),
            servers=servers,
        )

    def close_agent_endpoints(self, handle: TrialHandle) -> None:
        if self._gateways.close(handle.session_id) or not remote_lab():
            return
        from nika.remote.client import RemoteClient

        RemoteClient().mcp_detach(handle.session_id)

    def fault_check(self, handle: TrialHandle) -> FaultCheck:
        session_id = handle.session_id
        anomaly = anomaly_expected(handle.trial_path)
        bound = bound_injected_problem(session_id)
        if bound is None and not anomaly:
            return FaultCheck(
                result="not_applicable",
                fault="healthy",
                evidence={"reason": "no fault injected"},
            )
        if bound is None and anomaly and remote_lab():
            try:
                from nika.remote.client import RemoteClient

                raw = RemoteClient().fault_artifact(session_id)
            except Exception as exc:  # noqa: BLE001 - record a failed bounded read
                raw = {"present": False, "error": f"{type(exc).__name__}: {exc}"}
        else:
            raw = recheck_bound_artifact(session_id)
        error = str(raw.get("error") or "")
        if raw.get("present"):
            result = "present"
        elif raw.get("unbound") or (error and error != "fault artifact absent"):
            result = "error"
        else:
            result = "absent"
        return FaultCheck(
            result=result,
            fault=raw.get("fault"),
            evidence=raw.get("evidence") or {},
            error=raw.get("error"),
        )

    def score(
        self,
        handle: TrialHandle,
        *,
        result_dir: str | None = None,
        infra_evidence: bool = False,
    ) -> None:
        """Write ``eval_metrics.json`` into the trial directory."""
        run_eval_metrics(
            session_id=handle.session_id,
            result_dir=result_dir,
            session_dir=handle.trial_path,
            infra_evidence=infra_evidence,
        )

    def stop_trial(self, handle: TrialHandle, *, status: str = "finished") -> None:
        scheduler = self._schedulers.pop(handle.session_id, None)
        try:
            if scheduler is not None:
                scheduler.close()
        finally:
            self._stop_trial(handle, status=status)

    def _stop_trial(self, handle: TrialHandle, *, status: str) -> None:
        self._started.pop(handle.session_id, None)
        self._gateways.close(handle.session_id)
        close_session(
            session_id=handle.session_id,
            undeploy=True,
            session_dir=handle.trial_path,
            status=status,  # type: ignore[arg-type]
        )
