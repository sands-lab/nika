"""Unit tests for NIKA Remote config, artifacts, API, and workflow branching."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from nika.remote.api import create_remote_app
from nika.remote.artifacts import pack_session_dir, unpack_session_dir
from nika.remote.client import RemoteClient
from nika.remote.config import (
    ENV_REMOTE_ENABLED,
    ENV_REMOTE_SERVER,
    ENV_REMOTE_TOKEN,
    ENV_REMOTE_URL,
    RemoteConfig,
    is_remote_enabled,
    load_remote_config,
)
from nika.remote.handlers import handle_env_start
from nika.remote.protocol import EnvStartRequest, EnvStartResponse


@pytest.fixture(autouse=True)
def _clear_remote_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        ENV_REMOTE_ENABLED,
        ENV_REMOTE_URL,
        ENV_REMOTE_TOKEN,
        ENV_REMOTE_SERVER,
    ):
        monkeypatch.delenv(key, raising=False)


def test_is_remote_enabled_requires_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_REMOTE_ENABLED, "true")
    assert is_remote_enabled() is False
    monkeypatch.setenv(ENV_REMOTE_URL, "http://lab.example:8700")
    assert is_remote_enabled() is True


def test_is_remote_enabled_false_on_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_REMOTE_ENABLED, "true")
    monkeypatch.setenv(ENV_REMOTE_URL, "http://lab.example:8700")
    monkeypatch.setenv(ENV_REMOTE_SERVER, "1")
    assert is_remote_enabled() is False


def test_gateway_url_uses_remote_host() -> None:
    cfg = RemoteConfig(
        enabled=True,
        url="http://10.0.0.5:8700",
        token="",
        artifact_root="",
    )
    assert cfg.gateway_url(9123) == "http://10.0.0.5:9123"


def test_pack_unpack_session_dir(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "run.json").write_text('{"session_id":"s1"}', encoding="utf-8")
    nested = src / "nested"
    nested.mkdir()
    (nested / "gt.json").write_text("{}", encoding="utf-8")

    blob = pack_session_dir(src)
    dest = tmp_path / "dest"
    unpack_session_dir(blob, dest)
    assert (dest / "run.json").read_text(encoding="utf-8") == '{"session_id":"s1"}'
    assert (dest / "nested" / "gt.json").is_file()


def test_handle_env_start_logs(caplog: pytest.LogCaptureFixture) -> None:
    with (
        caplog.at_level(logging.INFO, logger="nika.remote"),
        patch(
            "nika.remote.handlers.start_net_env",
            return_value="sess-log",
        ),
        patch(
            "nika.remote.handlers.session_public_dict",
            return_value={"session_id": "sess-log"},
        ),
    ):
        handle_env_start(EnvStartRequest(scenario="simple_bgp"))
    messages = [r.getMessage() for r in caplog.records]
    assert any("env start begin" in m and "simple_bgp" in m for m in messages)
    assert any("env start done" in m and "sess-log" in m for m in messages)


def test_remote_app_health_and_auth() -> None:
    app = create_remote_app(token="secret")
    client = TestClient(app)
    assert client.get("/health").json()["status"] == "ok"
    denied = client.post("/v1/env/start", json={"scenario": "x"})
    assert denied.status_code == 401
    ok_headers = {"Authorization": "Bearer secret"}
    with patch(
        "nika.remote.api.handle_env_start",
        return_value=EnvStartResponse(
            session_id="sess-1",
            session={"session_id": "sess-1", "scenario_name": "x"},
        ),
    ):
        resp = client.post(
            "/v1/env/start",
            json={"scenario": "x"},
            headers=ok_headers,
        )
    assert resp.status_code == 200
    assert resp.json()["session_id"] == "sess-1"


def test_remote_app_artifacts(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "ground_truth.json").write_text("{}", encoding="utf-8")
    app = create_remote_app(token="")
    client = TestClient(app)
    with patch(
        "nika.remote.api.handle_artifacts",
        return_value=pack_session_dir(session_dir),
    ):
        resp = client.get("/v1/sessions/sess-1/artifacts")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/gzip")
    out = tmp_path / "out"
    unpack_session_dir(resp.content, out)
    assert (out / "ground_truth.json").is_file()


def test_remote_client_rewrites_gateway_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = RemoteConfig(
        enabled=True,
        url="http://lab.example:8700",
        token="",
        artifact_root="",
    )
    client = RemoteClient(cfg)

    def fake_request(method, path, **kwargs):
        assert method == "POST"
        assert path.endswith("/mcp/attach")
        return {
            "session_id": "s1",
            "gateway_port": 9555,
            "gateway_base_url": "http://0.0.0.0:9555",
        }

    monkeypatch.setattr(client, "_request", fake_request)
    attach = client.mcp_attach("s1", public_host="lab.example")
    assert attach.gateway_base_url == "http://lab.example:9555"
    assert attach.gateway_port == 9555


def test_start_net_env_remote_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_REMOTE_ENABLED, "true")
    monkeypatch.setenv(ENV_REMOTE_URL, "http://lab.example:8700")
    with patch(
        "nika.remote.workflows.remote_start_net_env", return_value="remote-sess"
    ) as mocked:
        from nika.workflows.env.start import start_net_env

        sid = start_net_env("simple_bgp", None)
    assert sid == "remote-sess"
    mocked.assert_called_once()


def test_mcp_policy_resource_from_url() -> None:
    from agent.sandbox.sbx.policy import mcp_policy_resource_from_url

    assert mcp_policy_resource_from_url("http://10.1.2.3:9444/mcp") == "10.1.2.3:9444"
    assert mcp_policy_resource_from_url("http://localhost", fallback_port=9) == (
        "localhost:9"
    )


def test_list_session_containers_remote_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_REMOTE_ENABLED, "true")
    monkeypatch.setenv(ENV_REMOTE_URL, "http://lab.example:8700")
    with patch(
        "nika.remote.workflows.remote_list_session_containers",
        return_value=("sess-1", "lab_abc", [{"name": "pc1"}]),
    ) as mocked:
        from nika.workflows.session.containers import list_session_containers

        sid, lab, rows = list_session_containers("sess-1")
    assert sid == "sess-1"
    assert lab == "lab_abc"
    assert rows == [{"name": "pc1"}]
    mocked.assert_called_once()


def test_remote_app_session_containers() -> None:
    from nika.remote.protocol import SessionContainersResponse

    app = create_remote_app(token="")
    client = TestClient(app)
    with patch(
        "nika.remote.api.handle_session_containers",
        return_value=SessionContainersResponse(
            session_id="sess-1",
            lab_name="lab_x",
            containers=[{"name": "pc1", "status": "running"}],
        ),
    ):
        resp = client.get("/v1/sessions/sess-1/containers")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "sess-1"
    assert body["lab_name"] == "lab_x"
    assert body["containers"][0]["name"] == "pc1"


def test_load_remote_config_requires_url_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_REMOTE_ENABLED, "1")
    # is_remote_enabled is false without URL; load still returns a default URL.
    cfg = load_remote_config()
    assert cfg.enabled is False

    monkeypatch.setenv(ENV_REMOTE_URL, "http://lab:8700")
    cfg = load_remote_config()
    assert cfg.enabled is True
    assert cfg.host == "lab"
