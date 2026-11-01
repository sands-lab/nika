"""Benchmark engine built into the current tree (contract B, in process).

Serves releases whose manifest declares no pinned ``engine``. The wrapped
workflows already forward to NIKA Remote when it is enabled.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from nika.config import BENCHMARK_VERSION
from nika.firecracker.trial import active_trial_lab
from nika.net_env.net_env_pool import scenario_requires_topo_size
from nika.benchmark_engine.protocol import (
    PROTOCOL_VERSIONS,
    AgentEndpoints,
    EngineDescription,
    FaultCheck,
    InjectResult,
    Resource,
    TrialHandle,
    TrialRequest,
)
from nika.validation.presence import (
    anomaly_expected,
    bound_injected_problem,
    recheck_bound_artifact,
    remote_lab,
)
from nika.workflows.benchmark.healthy import write_healthy_session_artifacts
from nika.workflows.env.start import start_net_env
from nika.workflows.eval.session import run_eval_metrics
from nika.workflows.failure.inject import inject_failure
from nika.workflows.session.close import close_session, load_session_meta_for_close

# Task-row keys forwarded to ``start_net_env`` as deployment parameters.
_DEPLOY_KEYS = ("topo", "igp", "bgp_mode", "rpki", "backend", "device_profile")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InTreeBenchmarkEngine:
    """Runs the current tree's environments, failures, and scoring."""

    def describe(self, release: str | None = None) -> EngineDescription:
        """Describe *release*; the in-tree engine serves every release in the tree."""
        from nika.workflows.benchmark.release import load_release, read_git_commit

        release = load_release(release or BENCHMARK_VERSION)
        commit, dirty = read_git_commit()
        return EngineDescription(
            protocol_versions=sorted(PROTOCOL_VERSIONS),
            release_id=release.id,
            release_version=release.version,
            engine_commit=commit,
            engine_dirty=dirty,
            task_file_sha256={
                str(meta["cases_file"]): _sha256(release.root / meta["cases_file"])
                for meta in release.splits.values()
            },
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
        if trial_dir is None:
            try:
                trial_dir = str(load_session_meta_for_close(session_id)["session_dir"])
            except BaseException:
                close_session(session_id=session_id, undeploy=True, status="error")
                raise
        return TrialHandle(
            session_id=session_id,
            trial_dir=trial_dir,
            resources=[Resource(kind="nika_session", id=session_id)],
            preinjected=image_lab is not None and image_lab.case is not None,
        )

    def inject(self, handle: TrialHandle, request: TrialRequest) -> InjectResult:
        if request.healthy:
            write_healthy_session_artifacts(handle.session_id)
        elif not handle.preinjected:
            inject_failure(
                problem_names=list(request.failures),
                session_id=handle.session_id,
                param_overrides=dict(request.inject),
                expected_root_causes=request.expected_root_causes,
            )
        return InjectResult(
            ground_truth=(handle.trial_path / "ground_truth.json").is_file()
        )

    def agent_endpoints(self, handle: TrialHandle) -> AgentEndpoints:
        from nika.mcp.gateway.access import node_roles_for_session
        from nika.mcp.gateway.lifecycle import resolve_session_backend
        from nika.mcp.registry import select_diagnosis_servers
        from nika.telemetry import telemetry_for_session
        from nika.utils.session_store import SessionStore

        row = SessionStore().get_session(handle.session_id)
        scenario = str(row.get("scenario_name") or "")
        return AgentEndpoints(
            diagnosis_servers=(
                select_diagnosis_servers(
                    scenario,
                    backend=row.get("backend") or resolve_session_backend(scenario),
                    telemetry=telemetry_for_session(row),
                )
                if scenario
                else None
            ),
            node_roles=node_roles_for_session(handle.session_id),
        )

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
        close_session(
            session_id=handle.session_id,
            undeploy=True,
            session_dir=handle.trial_path,
            status=status,  # type: ignore[arg-type]
        )
