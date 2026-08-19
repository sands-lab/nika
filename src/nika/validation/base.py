from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from nika.net_env.contract import ValidationContract, ValidationReport


class ValidationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    path: Path
    metadata: dict[str, Any]


class ValidationVerifier(Protocol):
    name: str
    supported_properties: frozenset[str]

    def verify(
        self, contract: ValidationContract, snapshot: ValidationSnapshot
    ) -> ValidationReport: ...
