"""Frontend client of a benchmark engine container (contract B)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, TypeVar

from pydantic import BaseModel

from nika.benchmark_engine.protocol import (
    API_VERSION,
    ENGINE_TOKEN_HEADER,
    AgentEndpoints,
    EngineDescription,
    EngineError,
    FaultCheck,
    GatewayRequest,
    InjectResult,
    TrialHandle,
    TrialRequest,
)

_M = TypeVar("_M", bound=BaseModel)

# Deploy and inject block server-side until the environment is ready, which
# takes up to 30 minutes for large environments.
_LIFECYCLE_TIMEOUT_SEC = 3600.0


class EngineClient:
    """Calls a benchmark engine over HTTP; implements ``LifecycleProtocol``."""

    def __init__(self, base_url: str, *, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _call(self, operation: str, payload: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}/v{API_VERSION}/{operation}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                ENGINE_TOKEN_HEADER: self.token,
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=_LIFECYCLE_TIMEOUT_SEC
            ) as resp:
                return json.loads(resp.read().decode() or "{}").get("result")
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode() or "{}")
            except json.JSONDecodeError:
                body = {}
            message = f"benchmark engine {operation} failed: {body.get('error') or exc}"
            if body.get("error_type") == "FileNotFoundError":
                raise FileNotFoundError(message) from exc
            raise EngineError(
                message, outcome=body.get("outcome") or "infra_failed"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise EngineError(
                f"benchmark engine at {self.base_url} is unreachable: {exc}"
            ) from exc

    def _model(self, model: type[_M], operation: str, payload: dict[str, Any]) -> _M:
        return model.model_validate(self._call(operation, payload))

    def describe(self, release: str | None = None) -> EngineDescription:
        return self._model(EngineDescription, "describe", {"release": release})

    def start_trial(self, request: TrialRequest) -> TrialHandle:
        return self._model(
            TrialHandle, "start_trial", {"request": request.model_dump(mode="json")}
        )

    def inject(self, handle: TrialHandle, request: TrialRequest) -> InjectResult:
        return self._model(
            InjectResult,
            "inject",
            {
                "handle": handle.model_dump(mode="json"),
                "request": request.model_dump(mode="json"),
            },
        )

    def agent_endpoints(
        self, handle: TrialHandle, request: GatewayRequest
    ) -> AgentEndpoints:
        return self._model(
            AgentEndpoints,
            "agent_endpoints",
            {
                "handle": handle.model_dump(mode="json"),
                "request": request.model_dump(mode="json"),
            },
        )

    def close_agent_endpoints(self, handle: TrialHandle) -> None:
        self._call("close_agent_endpoints", {"handle": handle.model_dump(mode="json")})

    def fault_check(self, handle: TrialHandle) -> FaultCheck:
        return self._model(
            FaultCheck, "fault_check", {"handle": handle.model_dump(mode="json")}
        )

    def score(
        self,
        handle: TrialHandle,
        *,
        result_dir: str | None = None,
        infra_evidence: bool = False,
    ) -> None:
        self._call(
            "score",
            {
                "handle": handle.model_dump(mode="json"),
                "result_dir": result_dir,
                "infra_evidence": infra_evidence,
            },
        )

    def stop_trial(self, handle: TrialHandle, *, status: str = "finished") -> None:
        # The agent sandbox runs on the frontend's host, outside the engine.
        _cleanup_sandbox(handle.session_id)
        self._call(
            "stop_trial", {"handle": handle.model_dump(mode="json"), "status": status}
        )


def _cleanup_sandbox(session_id: str) -> None:
    """Remove the trial's agent sandbox if a hard-killed agent left it behind."""
    from nika.workflows.session.close import cleanup_session_sandbox
    from nika.utils.session_store import SessionStore

    try:
        row = SessionStore().get_session(session_id)
    except FileNotFoundError:
        return
    cleanup_session_sandbox(row, session_id=session_id)
