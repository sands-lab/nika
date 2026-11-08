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
from nika.workflows.benchmark.healthy import write_healthy_session_artifacts
from nika.workflows.env.start import start_net_env
from nika.workflows.eval.session import run_eval_metrics
from nika.workflows.failure.inject import inject_failure
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
                telemetry=task.get("telemetry"),
                traffic=task.get("traffic"),
                **{key: task.get(key) for key in _DEPLOY_KEYS},
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
        """Make the trial ready for the agent, which starts right after.

        Injects the task's failure and returns once it is verified in place. A
        healthy task gets its no-fault artifacts, and a lab restored with its
        failure already injected needs nothing. Logs the post-inject fault
        read and returns the task the agent receives.
        """
        session_id = handle.session_id
        try:
            request, preinjected = self._started[session_id]
        except KeyError:
            raise EngineError(
                f"trial {session_id} was not started by this engine"
            ) from None
        if request.healthy:
            write_healthy_session_artifacts(session_id)
        elif not preinjected:
            inject_failure(
                problem_names=list(request.failures),
                session_id=session_id,
                param_overrides=dict(request.inject),
                expected_root_causes=request.expected_root_causes,
            )
        if bound_injected_problem(session_id) is None:
            # No injection in this process logged it: a healthy task, a lab
            # restored with its failure, or an injection on a NIKA Remote host.
            bind_session_dir(handle.trial_path)
            log_presence_check(presence_record(self.fault_check(handle), "post_inject"))
        session = Session().load_running_session(session_id=session_id)
        task_description = getattr(session, "task_description", None)
        if not task_description:
            raise EngineError(f"trial {session_id} has no task description")
        return AgentTask(task_description=task_description)

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
        self._started.pop(handle.session_id, None)
        self._gateways.close(handle.session_id)
        close_session(
            session_id=handle.session_id,
            undeploy=True,
            session_dir=handle.trial_path,
            status=status,  # type: ignore[arg-type]
        )
