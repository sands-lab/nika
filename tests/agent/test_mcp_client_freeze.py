"""Unit tests for sandbox → host diagnosis freeze via MCP gateway HTTP."""

from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from agent.sandbox.config import (
    ENV_GATEWAY_AGENT_URL,
    ENV_SANDBOX_EXECUTION,
    ENV_SESSION_DIR,
)
from agent.utils import mcp_client
from agent.utils.loggers import MESSAGES_FILENAME
from nika.mcp.gateway.session_registry import (
    advance_phase,
    clear_sessions,
    get_session,
    register_session,
)
from nika.workflows.agent.submission import (
    freeze_diagnosis,
    load_frozen_diagnosis_report,
)


def test_sandbox_begin_submission_freezes_on_host_via_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_session = tmp_path / "host_session"
    workspace = tmp_path / "sandbox_run"
    host_session.mkdir()
    workspace.mkdir()

    register_session(
        "sess-http-freeze",
        scenario_name="simple_bgp",
        session_dir=str(host_session),
    )
    monkeypatch.setenv(ENV_SANDBOX_EXECUTION, "1")
    monkeypatch.setenv(ENV_SESSION_DIR, str(workspace))
    monkeypatch.setenv(ENV_GATEWAY_AGENT_URL, "http://gateway.test")

    def fake_urlopen(request: Request, timeout: float = 10):
        assert request.full_url.endswith("/gateway/sessions/sess-http-freeze/phase")
        assert request.get_header("Nika-session-id") == "sess-http-freeze"
        body = json.loads(request.data.decode("utf-8"))
        assert body["phase"] == "submission"
        assert body["diagnosis_report"] == "pc1 eth0 down"

        # Simulate the host gateway freeze handler.
        freeze_diagnosis("sess-http-freeze", body["diagnosis_report"])
        advance_phase("sess-http-freeze", "submission")

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": true}'

        return _Resp()

    monkeypatch.setattr(mcp_client.urllib.request, "urlopen", fake_urlopen)

    try:
        mcp_client.begin_submission_mcp_phase("sess-http-freeze", "pc1 eth0 down")
        assert load_frozen_diagnosis_report("sess-http-freeze") == "pc1 eth0 down"
        assert get_session("sess-http-freeze").phase == "submission"

        workspace_event = json.loads(
            (workspace / MESSAGES_FILENAME).read_text(encoding="utf-8").strip()
        )
        assert workspace_event["event"] == "diagnosis_frozen"
        assert workspace_event["report"] == "pc1 eth0 down"

        host_event = json.loads(
            (host_session / MESSAGES_FILENAME).read_text(encoding="utf-8").strip()
        )
        assert host_event["event"] == "diagnosis_frozen"
        assert host_event["report"] == "pc1 eth0 down"
    finally:
        clear_sessions()


def test_sandbox_http_advance_omits_empty_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "sandbox_run"
    workspace.mkdir()
    monkeypatch.setenv(ENV_SANDBOX_EXECUTION, "1")
    monkeypatch.setenv(ENV_SESSION_DIR, str(workspace))
    monkeypatch.setenv(ENV_GATEWAY_AGENT_URL, "http://gateway.test")

    captured: dict = {}

    def fake_urlopen(request: Request, timeout: float = 10):
        captured["body"] = json.loads(request.data.decode("utf-8"))

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return _Resp()

    monkeypatch.setattr(mcp_client.urllib.request, "urlopen", fake_urlopen)
    (workspace / MESSAGES_FILENAME).write_text(
        json.dumps({"event": "diagnosis_frozen", "report": "prior"}) + "\n",
        encoding="utf-8",
    )

    mcp_client.begin_submission_mcp_phase("sess-2", "")
    assert captured["body"] == {"phase": "submission"}
    assert "diagnosis_report" not in captured["body"]


def test_http_advance_surfaces_gateway_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "sandbox_run"
    workspace.mkdir()
    monkeypatch.setenv(ENV_SANDBOX_EXECUTION, "1")
    monkeypatch.setenv(ENV_SESSION_DIR, str(workspace))
    monkeypatch.setenv(ENV_GATEWAY_AGENT_URL, "http://gateway.test")

    def fake_urlopen(request: Request, timeout: float = 10):
        raise HTTPError(
            request.full_url,
            400,
            "Bad Request",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"error":"diagnosis_report is required"}'),
        )

    monkeypatch.setattr(mcp_client.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="diagnosis_report is required"):
        mcp_client.begin_submission_mcp_phase("sess-3", "report text")
