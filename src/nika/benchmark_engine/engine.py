"""Benchmark engine of release 0.1.0: the lifecycle operations of one trial.

Each operation runs the code release 0.1.0's benchmark runner ran for that
step (``run_single_case``): ``start_trial`` deploys the environment,
``begin`` injects the failure and returns the task description, the trial's
MCP gateway serves this release's tools, and ``stop_trial`` closes the
session. The frontend runs the agent and writes the run records.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from nika.benchmark_engine.host import local_host_info
from nika.benchmark_engine.protocol import (
    API_VERSIONS,
    AgentEndpoints,
    AgentTask,
    EngineDescription,
    EngineError,
    FaultCheck,
    GatewayRequest,
    Resource,
    Scores,
    TaskList,
    TrialHandle,
    TrialRequest,
)

# Commit an engine image was built from (see ``docker/Dockerfile``).
ENV_ENGINE_COMMIT = "NIKA_ENGINE_COMMIT"
# The only release this engine serves.
RELEASE_VERSION = "0.1.0"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_release(release: str | None) -> str:
    if release not in (None, RELEASE_VERSION):
        raise EngineError(
            f"This benchmark engine serves release {RELEASE_VERSION}, not {release!r}."
        )
    return RELEASE_VERSION


def _allows_everything(request: GatewayRequest) -> bool:
    if request.access is None:
        return True
    policy = request.access.policy
    return policy.tools == ["*"] and policy.node_roles == ["*"] and not policy.node_ids


class BenchmarkEngine:
    """Runs release 0.1.0's environments, failures, MCP gateway, and scoring."""

    def __init__(self) -> None:
        from nika.service.mcp_gateway.leases import GatewayLeases

        self._gateways = GatewayLeases()
        # Started trials, by session id: the request ``begin`` injects.
        self._started: dict[str, TrialRequest] = {}

    def describe(self, release: str | None = None) -> EngineDescription:
        from nika.workflows.benchmark.release import load_release, read_git_commit

        loaded = load_release(_require_release(release))
        commit, dirty = read_git_commit()
        if commit is None:
            # An engine image: built from a clean commit, without ``.git``.
            commit = os.environ.get(ENV_ENGINE_COMMIT) or None
        return EngineDescription(
            api_versions=sorted(API_VERSIONS),
            release_id=loaded.id,
            release_version=loaded.version,
            engine_commit=commit,
            engine_dirty=dirty,
            task_file_sha256={
                str(meta["cases_file"]): _sha256(loaded.root / meta["cases_file"])
                for meta in loaded.splits.values()
            },
            host=local_host_info(),
        )

    def list_tasks(self, release: str, split: str) -> TaskList:
        """List the split's tasks once this release's preflight passes."""
        from nika.benchmark_engine.images import ensure_lab_images
        from nika.benchmark_engine.tasks import task_info
        from nika.workflows.benchmark.release import load_release, preflight_release

        loaded = load_release(_require_release(release), split=split)
        preflight_release(loaded, check_images=False)
        ensure_lab_images()
        return TaskList(
            release=loaded.version,
            split=loaded.split,
            task_file_sha256=loaded.cases_sha256,
            tasks=[task_info(row) for row in loaded.cases],
        )

    def start_trial(self, request: TrialRequest) -> TrialHandle:
        """Deploy the task's environment as the release runner did."""
        from nika.benchmark_engine.tasks import resolve_trial
        from nika.net_env.net_env_pool import scenario_requires_topo_size
        from nika.utils.agent_session_id import make_agent_session_id
        from nika.utils.session import Session
        from nika.workflows.benchmark.resume import (
            benchmark_row_fingerprint,
            benchmark_row_from_case,
        )
        from nika.workflows.benchmark.run import validate_inject_params
        from nika.workflows.env.start import start_net_env
        from nika.workflows.session.close import close_session

        _require_release(request.release)
        request = resolve_trial(request)
        if not request.session_id:
            raise EngineError("start_trial needs the session_id the frontend chose")
        if not request.trial_dir or not Path(request.trial_dir).is_absolute():
            raise EngineError("start_trial needs an absolute trial_dir")
        row = request.task
        assert row is not None
        scenario = str(row["scenario"])
        topo_size = row.get("topo_size") or ""
        size = topo_size or None
        if scenario_requires_topo_size(scenario) and not size:
            raise EngineError(f"Scenario {scenario!r} requires a topology size.")
        if not scenario_requires_topo_size(scenario):
            size = None
        validate_inject_params(row["problem"], scenario, topo_size, request.inject)

        session_id = start_net_env(
            scenario,
            size,
            redeploy=True,
            result_dir=request.result_dir,
            session_tag=request.session_tag,
            session_id=request.session_id,
            session_dir=request.trial_dir,
        )
        try:
            session = Session().load_running_session(session_id=session_id)
            # The session ID names the task; the agent gets this one instead.
            session.update_session("agent_session_id", make_agent_session_id())
            session.update_session("benchmark_version", request.release)
            session.update_session(
                "benchmark_fingerprint",
                benchmark_row_fingerprint(
                    benchmark_row_from_case(
                        scenario=scenario,
                        problem=row["problem"],
                        topo_size=topo_size,
                        inject_params=request.inject,
                    )
                ),
            )
        except BaseException:
            close_session(
                session_id=session_id,
                undeploy=True,
                session_dir=request.trial_dir,
                status="error",
            )
            raise
        self._started[session_id] = request
        return TrialHandle(
            session_id=session_id,
            trial_dir=request.trial_dir,
            resources=[Resource(kind="nika_session", id=session_id)],
        )

    def begin(self, handle: TrialHandle) -> AgentTask:
        """Inject the task's failure; return the task the agent receives."""
        from nika.utils.session import Session
        from nika.workflows.failure.inject import inject_failure

        request = self._started.get(handle.session_id)
        if request is None:
            raise EngineError(
                f"trial {handle.session_id} was not started by this engine"
            )
        inject_failure(
            problem_names=list(request.failures),
            session_id=handle.session_id,
            param_overrides=dict(request.inject),
            expected_root_causes=request.expected_root_causes,
        )
        session = Session().load_running_session(session_id=handle.session_id)
        task_description = getattr(session, "task_description", None)
        if not task_description:
            raise EngineError(f"trial {handle.session_id} has no task description")
        return AgentTask(task_description=task_description)

    def agent_endpoints(
        self, handle: TrialHandle, request: GatewayRequest
    ) -> AgentEndpoints:
        """Open the trial's MCP gateway, which serves this release's tools."""
        from agent.utils.mcp_servers import select_session_servers
        from nika.runtime.factory import resolve_backend
        from nika.utils.session_store import SessionStore

        if request.tool_call_limits:
            raise EngineError(
                f"Release {RELEASE_VERSION} has no tool-call limits; run it without "
                "agent.tool_call_limits.",
            )
        if not _allows_everything(request):
            raise EngineError(
                f"Release {RELEASE_VERSION} has no diagnosis access policy; run it "
                "with the default access, which allows every tool and node.",
            )
        row = SessionStore().get_session(handle.session_id)
        scenario = str(row.get("scenario_name") or "")
        backend = resolve_backend(row)
        lease = self._gateways.open(
            handle.session_id,
            scenario_name=scenario,
            session_dir=handle.trial_dir,
            backend=backend,
            bind_host=request.bind_host,
            sandbox=request.sandbox,
            agent_host=request.agent_host,
            agent_session_id=row.get("agent_session_id"),
        )
        return AgentEndpoints(
            gateway_url=lease.manager.base_url,
            agent_url=lease.agent_url,
            session_token=lease.access_token,
            phase_token=lease.phase_token,
            servers=select_session_servers(scenario, backend=backend),
        )

    def close_agent_endpoints(self, handle: TrialHandle) -> None:
        self._gateways.close(handle.session_id)

    def fault_check(self, handle: TrialHandle) -> FaultCheck:
        """Release 0.1.0 verifies its failure once, at injection, and no later.

        Its verification may send probe traffic, so rerunning it during the
        agent's turn would change what the agent observes.
        """
        request = self._started.get(handle.session_id)
        return FaultCheck(
            result="error",
            fault=request.failures[0] if request and request.failures else None,
            error=f"release {RELEASE_VERSION} has no fault presence recheck",
        )

    def score(self, handle: TrialHandle, *, infra_evidence: bool = False) -> Scores:
        from nika.benchmark_engine.scores import grade_trial
        from nika.workflows.benchmark.release import load_release

        scoring_id = str(load_release(RELEASE_VERSION).scoring.get("id") or "")
        return grade_trial(
            handle.trial_path, scoring_id=scoring_id, infra_evidence=infra_evidence
        )

    def stop_trial(self, handle: TrialHandle, *, status: str = "finished") -> None:
        """Close the trial's gateway and session; repeating it is harmless."""
        from nika.workflows.session.close import close_session

        self._started.pop(handle.session_id, None)
        self._gateways.close(handle.session_id)
        try:
            close_session(
                session_id=handle.session_id,
                undeploy=True,
                session_dir=handle.trial_dir,
                status=status,
            )
        except FileNotFoundError:
            pass
