"""Status rules for a full environment audit of one benchmark case.

``pass`` is the only status that admits a case. ``skipped``, ``unsupported``,
``no_evidence``, ``not_run``, and ``fail`` stay distinct and do not admit.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from scripts.benchmark_audit.provenance import AuditProvenance
from nika.workflows.benchmark.resume import benchmark_row_fingerprint
from nika.workflows.benchmark.multi_fault import row_problems
from nika.problems.multi import fault_instance_keys

AuditStatus = Literal[
    "pass",
    "fail",
    "skipped",
    "unsupported",
    "no_evidence",
    "not_run",
]

# Higher rank wins when several stage statuses are combined.
_STATUS_RANK: dict[str, int] = {
    "pass": 0,
    "not_run": 1,
    "skipped": 2,
    "unsupported": 3,
    "no_evidence": 4,
    "fail": 5,
}

_REASON_STATUS: dict[str, AuditStatus] = {
    "no_verify_lab": "unsupported",
    "no_probe_path": "no_evidence",
    "custom_requires_problem_instance": "no_evidence",
    "no_control_path": "unsupported",
    "undeclared_symptom": "unsupported",
    "not_run": "not_run",
}


class CaseIdentity(BaseModel):
    """The fields that name one benchmark case."""

    model_config = ConfigDict(extra="forbid")

    scenario: str
    topo_size: str = ""
    backend: str = ""
    igp: str = ""
    bgp_mode: str = ""
    rpki: str = ""
    device_profile: str = ""
    fault: str
    problems: list[str] = Field(default_factory=list)
    objectives: list[str] | None = None
    schedule: dict[str, Any] | None = None
    inject: dict[str, Any] = Field(default_factory=dict)
    # Background traffic profile of the case; empty when it runs without one.
    traffic: dict[str, Any] = Field(default_factory=dict)
    # Telemetry kinds the case deploys; empty when it runs without telemetry.
    telemetry: list[str] = Field(default_factory=list)

    def key(self) -> str:
        """The benchmark row fingerprint of this case."""
        return benchmark_row_fingerprint(
            {
                "scenario": self.scenario,
                "problem": self.fault,
                "topo_size": self.topo_size,
                "igp": self.igp,
                "bgp_mode": self.bgp_mode,
                "rpki": self.rpki.lower() == "true",
                "backend": self.backend,
                "device_profile": self.device_profile,
                "inject": self.inject,
                "traffic": self.traffic or None,
                "objectives": self.objectives,
                **({"problems": self.problems} if self.problems else {}),
                **({"schedule": self.schedule} if self.schedule is not None else {}),
                **({"telemetry": self.telemetry} if self.telemetry else {}),
            }
        )


class StageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    status: AuditStatus
    reason: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    started_at: str | None = None
    duration_sec: float | None = None


class CaseAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: CaseIdentity
    stages: list[StageResult] = Field(default_factory=list)
    symptom_probe: str = ""
    provenance: AuditProvenance | None = None
    # "native": a fresh deployment with injection; "case_image": the restored
    # Firecracker case image that trials start from.
    mode: Literal["native", "case_image"] = "native"

    def admission(self) -> AuditStatus:
        required_stages = (
            {"baseline_lab", "agent_view", "oracle", "final_lab"}
            if self.identity.fault == "healthy"
            else {
                "image_artifact",
                "image_symptom",
                "oracle",
                "neighbor_oracle",
                "image_persistence_artifact",
                "image_persistence_symptom",
                "image_final_artifact",
                "image_final_symptom",
            }
            if self.mode == "case_image"
            else {
                "baseline_lab",
                "baseline_path",
                "sham_symptom",
                "sham_oracle",
                "baseline_agent_view",
                "inject_artifact",
                "leak_scan",
                "symptom",
                "oracle",
                "neighbor_oracle",
                "persistence_artifact",
                "persistence_symptom",
                "final_artifact",
                "final_symptom",
            }
        )
        if len(self.identity.problems) > 1 and self.mode == "native":
            required_stages = {
                "baseline_lab",
                "baseline_agent_view",
                "combination_matrix",
                "sham_oracle",
                *(
                    f"neighbor_oracle:{key}"
                    for key in fault_instance_keys(self.identity.problems)
                ),
                "timeline",
                "inject_artifact",
                "leak_scan",
                "symptom",
                "oracle",
                "recovery",
                "final_lab",
            }
        if len(self.identity.problems) > 1 and self.mode == "case_image":
            required_stages.discard("neighbor_oracle")
            required_stages.update(
                f"neighbor_oracle:{key}"
                for key in fault_instance_keys(self.identity.problems)
            )
        if self.mode == "case_image" and self.identity.schedule is not None:
            required_stages.add("timeline")
        # A control path is required when the topology has one. When every
        # peer path crosses the root cause, the stage records no_control_path
        # with the reason, and the baseline, negative control, and repeated
        # symptom reads still establish the effect. Likewise a scenario may
        # support none of the failure's look-alikes.
        exempt = {
            ("control_path", "no_control_path"),
            ("neighbor_oracle", "no_confusable_on_scenario"),
        }
        required = [
            stage.status
            for stage in self.stages
            if not (
                stage.status == "unsupported"
                and (
                    (stage.stage, stage.reason) in exempt
                    or (
                        stage.stage.startswith("neighbor_oracle:")
                        and stage.reason == "no_confusable_on_scenario"
                    )
                )
            )
        ]
        if (
            required_stages - {stage.stage for stage in self.stages}
            and admission_status(required) == "pass"
        ):
            required.append("no_evidence")
        return admission_status(required)


def admission_status(statuses: list[str]) -> AuditStatus:
    """Combine stage statuses. An empty list is ``not_run``."""
    if not statuses:
        return "not_run"
    unknown = [item for item in statuses if item not in _STATUS_RANK]
    if unknown:
        raise ValueError(f"unknown audit status: {unknown[0]}")
    return max(statuses, key=lambda item: _STATUS_RANK[item])  # type: ignore[return-value]


def classify_observation(
    payload: dict[str, Any] | None, *, ok: bool | None = None
) -> tuple[AuditStatus, str | None]:
    """Map a probe payload to an audit status.

    ``skipped``, ``unsupported``, and a missing payload
    do not become ``pass``, even when a caller also passes ``ok=True``.
    """
    if not isinstance(payload, dict) or not payload:
        return "no_evidence", "missing observation"
    reason = payload.get("reason")
    reason_text = str(reason) if reason else ""
    if payload.get("skipped") or reason_text in _REASON_STATUS:
        key = reason_text or "skipped"
        return _REASON_STATUS.get(key, "skipped"), key
    error = payload.get("error") or _nested_error(payload)
    if error:
        error_text = str(error)
        mapped = _REASON_STATUS.get(error_text)
        if mapped is not None:
            return mapped, error_text
        return "fail", error_text
    if ok is False or payload.get("verified") is False:
        return "fail", "observation failed"
    if ok is True or payload.get("verified") is True:
        return "pass", None
    return "no_evidence", "missing observation"


def _nested_error(payload: dict[str, Any]) -> str | None:
    """An error reported by a before, after, or details snapshot."""
    for key in ("before", "after", "details"):
        inner = payload.get(key)
        if isinstance(inner, dict) and inner.get("error"):
            return str(inner["error"])
    return None


def identity_from_row(row: dict[str, Any]) -> CaseIdentity:
    """Build a case identity from a release or benchmark row."""
    inject = row.get("inject") or {}
    rpki = row.get("rpki")
    return CaseIdentity(
        scenario=str(row.get("scenario") or ""),
        topo_size=str(row.get("topo_size") or ""),
        backend=str(row.get("backend") or ""),
        igp=str(row.get("igp") or ""),
        bgp_mode=str(row.get("bgp_mode") or ""),
        rpki="" if rpki is None else str(rpki),
        device_profile=str(row.get("device_profile") or ""),
        fault=str(
            row.get("problem") or row.get("fault") or "+".join(row_problems(row))
        ),
        problems=row_problems(row) if len(row_problems(row)) > 1 else [],
        schedule=row.get("schedule"),
        objectives=row.get("objectives"),
        inject=dict(inject) if isinstance(inject, dict) else {},
        traffic=dict(row.get("traffic") or {}),
        telemetry=list(row.get("telemetry") or []),
    )
