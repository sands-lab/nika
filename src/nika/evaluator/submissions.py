"""Pydantic schemas for agent submission and ground-truth fields used in eval."""

from __future__ import annotations

import textwrap

from pydantic import BaseModel, Field

from nika.problems.root_cause import RootCause


class DetectionSubmission(BaseModel):
    is_anomaly: bool = Field(description="Indicates whether an anomaly was detected.")


class LocalizationSubmission(BaseModel):
    faulty_devices: list[str] = Field(
        ...,
        description=textwrap.dedent("""\
            List of localized devices that are identified as faulty. Each item is a device name (string).
            Example: ["router_1", "switch_2"]
        """),
    )


class RCASubmission(BaseModel):
    root_cause_name: list[str] = Field(
        ...,
        description="The name(s) of the identified root cause(s) of the network anomaly.",
    )


class RootCauseSubmission(BaseModel):
    root_causes: list[RootCause] = Field(
        default_factory=list,
        description="Structured diagnoses: each item is resource + fault_type.",
    )
