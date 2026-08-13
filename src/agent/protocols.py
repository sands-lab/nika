"""Shared agent interfaces and pipeline phase identifiers."""

from typing import Any, Protocol, runtime_checkable

DIAGNOSIS = "diagnosis"
SUBMISSION = "submission"
PHASES = (DIAGNOSIS, SUBMISSION)


@runtime_checkable
class TroubleshootingAgent(Protocol):
    """Contract every agent implementation must satisfy."""

    session_id: str

    async def run(self, task_description: str) -> dict[str, Any]: ...
