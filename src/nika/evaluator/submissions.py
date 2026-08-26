"""Pydantic schemas for agent submission and ground-truth fields used in eval."""

from __future__ import annotations

from pydantic import BaseModel, Field

from nika.problems.rca import RootCause


class DetectionSubmission(BaseModel):
    is_anomaly: bool = Field(description="Indicates whether an anomaly was detected.")


class RootCauseSubmission(BaseModel):
    root_causes: list[RootCause] = Field(
        default_factory=list,
        description="Structured diagnoses: each item is resource + fault_type.",
    )
