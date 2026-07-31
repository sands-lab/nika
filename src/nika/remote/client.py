"""HTTP client for the NIKA Remote lab-host daemon."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin

from nika.remote.artifacts import unpack_session_dir
from nika.remote.config import RemoteConfig, load_remote_config
from nika.remote.protocol import (
    EnvStartRequest,
    EnvStartResponse,
    FailureInjectRequest,
    FailureInjectResponse,
    HealthResponse,
    McpAttachRequest,
    McpAttachResponse,
    PolicyMode,
    SessionCloseRequest,
    SessionContainersResponse,
)


class RemoteError(RuntimeError):
    """Raised when the remote daemon returns an error or is unreachable."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RemoteClient:
    """Synchronous HTTP client for lab-side remote operations."""

    def __init__(self, config: RemoteConfig | None = None) -> None:
        self.config = config or load_remote_config()

    def _headers(
        self, *, content_type: str | None = "application/json"
    ) -> dict[str, str]:
        headers: dict[str, str] = {}
        if content_type:
            headers["Content-Type"] = content_type
        if self.config.token:
            headers["Authorization"] = f"Bearer {self.config.token}"
        return headers

    def _url(self, path: str) -> str:
        return urljoin(self.config.base_url + "/", path.lstrip("/"))

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: float = 600.0,
        expect_json: bool = True,
    ) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self._url(path),
            data=data,
            headers=self._headers(
                content_type="application/json" if body is not None else None
            ),
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not expect_json:
                    return raw
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(detail)
                message = payload.get("error") or detail
            except json.JSONDecodeError:
                message = detail or str(exc)
            raise RemoteError(
                f"Remote {method} {path} failed ({exc.code}): {message}",
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise RemoteError(
                f"Cannot reach NIKA remote at {self.config.base_url}: {exc.reason}"
            ) from exc

    def health(self) -> HealthResponse:
        return HealthResponse.model_validate(
            self._request("GET", "/health", timeout=10.0)
        )

    def env_start(self, request: EnvStartRequest) -> EnvStartResponse:
        return EnvStartResponse.model_validate(
            self._request("POST", "/v1/env/start", body=request.model_dump())
        )

    def failure_inject(self, request: FailureInjectRequest) -> FailureInjectResponse:
        return FailureInjectResponse.model_validate(
            self._request("POST", "/v1/failure/inject", body=request.model_dump())
        )

    def mcp_attach(
        self,
        session_id: str,
        *,
        policy_mode: PolicyMode = "two_phase",
        public_host: str | None = None,
    ) -> McpAttachResponse:
        body = McpAttachRequest(
            policy_mode=policy_mode,
            public_host=public_host or self.config.host,
        )
        resp = McpAttachResponse.model_validate(
            self._request(
                "POST",
                f"/v1/sessions/{session_id}/mcp/attach",
                body=body.model_dump(),
            )
        )
        # Prefer a host the local agent can reach (from NIKA_REMOTE_URL).
        return McpAttachResponse(
            session_id=resp.session_id,
            gateway_port=resp.gateway_port,
            gateway_base_url=self.config.gateway_url(resp.gateway_port),
        )

    def mcp_detach(self, session_id: str) -> None:
        self._request("POST", f"/v1/sessions/{session_id}/mcp/detach", body={})

    def close_session(
        self,
        session_id: str | None = None,
        *,
        undeploy: bool = True,
        stop_all: bool = False,
    ) -> None:
        body = SessionCloseRequest(undeploy=undeploy, stop_all=stop_all).model_dump()
        if stop_all:
            self._request("POST", "/v1/sessions/wipe", body=body)
            return
        if not session_id:
            raise ValueError("session_id is required unless stop_all=True")
        self._request("POST", f"/v1/sessions/{session_id}/close", body=body)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{session_id}")

    def session_containers(self, session_id: str) -> SessionContainersResponse:
        return SessionContainersResponse.model_validate(
            self._request("GET", f"/v1/sessions/{session_id}/containers")
        )

    def list_sessions(self, *, running_only: bool = True) -> list[dict[str, Any]]:
        q = "running_only=true" if running_only else "running_only=false"
        payload = self._request("GET", f"/v1/sessions?{q}")
        sessions = payload.get("sessions", payload)
        if not isinstance(sessions, list):
            raise RemoteError("Unexpected /v1/sessions response shape")
        return sessions

    def pull_artifacts(self, session_id: str, dest_dir: str) -> None:
        raw = self._request(
            "GET",
            f"/v1/sessions/{session_id}/artifacts",
            expect_json=False,
            timeout=600.0,
        )
        if not isinstance(raw, (bytes, bytearray)):
            raise RemoteError("Artifact response was not binary")
        unpack_session_dir(bytes(raw), dest_dir)
