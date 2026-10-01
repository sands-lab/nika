"""Status rules for a full environment audit of one benchmark case.

``pass`` is the only status that admits a case. ``skipped``, ``unsupported``,
``no_evidence``, ``not_run``, and ``fail`` stay distinct and do not admit.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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
    "artifact_only": "no_evidence",
    "control_plane_only": "unsupported",
    "no_verify_lab": "unsupported",
    "no_probe_path": "no_evidence",
    "custom_requires_problem_instance": "no_evidence",
    "no_control_path": "unsupported",
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
    inject: dict[str, Any] = Field(default_factory=dict)

    def key(self) -> tuple[str, ...]:
        inject_items = tuple(
            f"{name}={self.inject[name]}" for name in sorted(self.inject)
        )
        return (
            self.scenario,
            self.topo_size,
            self.backend,
            self.igp,
            self.bgp_mode,
            self.rpki,
            self.device_profile,
            self.fault,
            *inject_items,
        )


class StageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    status: AuditStatus
    reason: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class CaseAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: CaseIdentity
    stages: list[StageResult] = Field(default_factory=list)
    symptom_probe: str = ""
    method_version: int = 1

    def admission(self) -> AuditStatus:
        required_stages = (
            {"baseline_lab", "final_lab"}
            if self.identity.fault == "healthy"
            else {
                "baseline_lab",
                "baseline_path",
                "inject_artifact",
                "symptom",
                "persistence_artifact",
                "persistence_symptom",
                "final_artifact",
                "final_symptom",
            }
        )
        # A separate sibling path is useful evidence when available. Some
        # faults have no independent sibling; their healthy baseline and
        # repeated symptom observations still establish the network effect.
        required = [
            stage.status
            for stage in self.stages
            if not (
                stage.stage == "control_path"
                and stage.status == "unsupported"
                and stage.reason == "no_control_path"
            )
        ]
        if (
            required_stages - {stage.stage for stage in self.stages}
            and admission_status(required) == "pass"
        ):
            required.append("no_evidence")
        return admission_status(required)


def admits(status: str) -> bool:
    """True only for a full-audit pass."""
    return status == "pass"


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

    ``artifact_only``, ``skipped``, ``unsupported``, and a missing payload
    do not become ``pass``, even when a caller also passes ``ok=True``.
    """
    if not isinstance(payload, dict) or not payload:
        return "no_evidence", "missing observation"
    reason = payload.get("reason")
    reason_text = str(reason) if reason else ""
    if payload.get("skipped") or reason_text in _REASON_STATUS:
        key = reason_text or "skipped"
        return _REASON_STATUS.get(key, "skipped"), key
    error = payload.get("error")
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
    if "comparison" in payload or "checks" in payload:
        return "pass", None
    return "no_evidence", "missing observation"


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
        fault=str(row.get("problem") or row.get("fault") or ""),
        inject=dict(inject) if isinstance(inject, dict) else {},
    )
